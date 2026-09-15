"""Scheduled weekly funnel report: refresh the data, render HTML, email it.

This is the funnel analysis refactored into something that runs unattended.
A scheduled job has two failure modes an interactive script does not -- it can
break silently, and it can report a number nobody checks -- so this one logs
every step and exits non-zero when anything goes wrong.

Entry point for cron: scripts/run_weekly.sh
"""
import base64
import io
import os
import sqlite3
import sys
import traceback
from datetime import date, datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "data" / "funnel.db"
REPORT_DIR = PROJECT_ROOT / "reports"

load_dotenv(PROJECT_ROOT / ".env")

# Crossing one of these puts a banner at the top of the report.
BOT_SHARE_ALERT = 0.80
MIN_HUMAN_SESSIONS = 20


def totals(connection, start, end):
    row = connection.execute(
        """
        SELECT COALESCE(SUM(sessions),0), COALESCE(SUM(cart_add_sessions),0),
               COALESCE(SUM(checkout_sessions),0), COALESCE(SUM(completed_sessions),0)
        FROM session_metrics_daily
        WHERE dimension = 'day' AND date BETWEEN ? AND ?
        """,
        (start, end),
    ).fetchone()
    return dict(zip(("sessions", "carts", "checkouts", "orders"), row))


def bot_split(connection, start, end):
    out = {"human": 0, "bot": 0, "human_carts": 0}
    for value, sessions, carts in connection.execute(
        """
        SELECT LOWER(dimension_value), SUM(sessions), SUM(cart_add_sessions)
        FROM session_metrics_daily
        WHERE dimension = 'human_or_bot_session' AND date BETWEEN ? AND ?
        GROUP BY LOWER(dimension_value)
        """,
        (start, end),
    ):
        if "human" in value:
            out["human"] += sessions
            out["human_carts"] += carts
        elif "bot" in value:
            out["bot"] += sessions
    return out


def entry_split(connection, start, end):
    return connection.execute(
        """
        SELECT dimension_value, SUM(sessions), SUM(cart_add_sessions)
        FROM session_metrics_daily
        WHERE dimension = 'landing_page_type+human_or_bot_session'
          AND LOWER(dimension_value) LIKE '%human%'
          AND date BETWEEN ? AND ?
        GROUP BY dimension_value
        HAVING SUM(sessions) > 0
        ORDER BY SUM(sessions) DESC
        """,
        (start, end),
    ).fetchall()


def weekly_series(connection, end: date, weeks: int = 10):
    """One row per week: total sessions and human sessions."""
    out = []
    for i in range(weeks - 1, -1, -1):
        stop = end - timedelta(days=7 * i)
        start = stop - timedelta(days=6)
        t = totals(connection, start.isoformat(), stop.isoformat())
        b = bot_split(connection, start.isoformat(), stop.isoformat())
        out.append((stop, t["sessions"], b["human"], t["carts"]))
    return out


def pct(a, b):
    return f"{a / b * 100:.2f}%" if b else "—"


def delta(now, before):
    if before == 0:
        return "—" if now == 0 else f"+{now}"
    return f"{now - before:+d} ({(now - before) / before * 100:+.0f}%)"


def chart_base64(series) -> str:
    """Weekly trend as an inline data URI, so the email needs no attachments."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    labels = [d.strftime("%m/%d") for d, _s, _h, _c in series]
    figure, axes = plt.subplots(figsize=(8.6, 3.4))
    x = range(len(series))
    axes.bar(x, [s for _d, s, _h, _c in series], color="#d8d2c4", label="all sessions")
    axes.bar(x, [h for _d, _s, h, _c in series], color="#c9a227", label="human sessions")
    axes.set_xticks(list(x))
    axes.set_xticklabels(labels, fontsize=8)
    axes.set_ylabel("sessions")
    axes.set_title("Weekly sessions — human vs total", fontsize=12, pad=10)
    axes.legend(frameon=False, fontsize=9)
    axes.spines[["top", "right"]].set_visible(False)
    figure.tight_layout()

    buffer = io.BytesIO()
    figure.savefig(buffer, format="png", dpi=140)
    plt.close(figure)
    return base64.b64encode(buffer.getvalue()).decode()


def render(connection) -> tuple[str, str, Path]:
    latest = connection.execute(
        "SELECT MAX(date) FROM session_metrics_daily WHERE dimension = 'day'"
    ).fetchone()[0]
    if not latest:
        raise RuntimeError("No daily rows in the database — the pull wrote nothing.")

    end = date.fromisoformat(latest)
    this_start, last_start, last_end = (
        end - timedelta(days=6), end - timedelta(days=13), end - timedelta(days=7))
    iso = date.isoformat

    now = totals(connection, iso(this_start), iso(end))
    before = totals(connection, iso(last_start), iso(last_end))
    now_b = bot_split(connection, iso(this_start), iso(end))
    before_b = bot_split(connection, iso(last_start), iso(last_end))

    traffic = now_b["human"] + now_b["bot"]
    bot_share = now_b["bot"] / traffic if traffic else 0

    alerts = []
    if bot_share >= BOT_SHARE_ALERT:
        alerts.append(f"Bots were <b>{bot_share * 100:.0f}%</b> of traffic this week. "
                      f"Read every pooled number below with that in mind.")
    if now_b["human"] < MIN_HUMAN_SESSIONS:
        alerts.append(f"Only <b>{now_b['human']} human sessions</b> this week. "
                      f"Nothing here is measurable for effect.")
    if now["sessions"] == 0:
        alerts.append("<b>Zero sessions recorded.</b> Check the store and the pull.")

    rows = "".join(
        f"<tr><td>{label}</td><td class=n>{a}</td><td class=n>{b}</td><td class=n>{delta(a, b)}</td></tr>"
        for label, a, b in (
            ("Sessions", now["sessions"], before["sessions"]),
            ("Added to cart", now["carts"], before["carts"]),
            ("Reached checkout", now["checkouts"], before["checkouts"]),
            ("Completed order", now["orders"], before["orders"]),
        )
    )
    who = "".join(
        f"<tr><td>{label}</td><td class=n>{a}</td><td class=n>{b}</td><td class=n>{delta(a, b)}</td></tr>"
        for label, a, b in (
            ("Human sessions", now_b["human"], before_b["human"]),
            ("Bot sessions", now_b["bot"], before_b["bot"]),
        )
    )
    entries = "".join(
        f"<tr><td>{str(v).split(' | ')[0]}</td><td class=n>{s}</td>"
        f"<td class=n>{c}</td><td class=n>{pct(c, s)}</td></tr>"
        for v, s, c in entry_split(connection, iso(this_start), iso(end))
    ) or "<tr><td colspan=4><em>No human sessions this week.</em></td></tr>"

    alert_html = ("<div class=alert><b>Alerts</b><ul>"
                  + "".join(f"<li>{a}</li>" for a in alerts) + "</ul></div>") if alerts else ""

    chart = chart_base64(weekly_series(connection, end))
    stored = connection.execute("SELECT COUNT(*) FROM session_metrics_daily").fetchone()[0]
    subject = (f"Pulse and Glow — weekly funnel, {iso(end)} "
               f"({now['sessions']} sessions, {now['orders']} orders)")

    html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{subject}</title>
<style>
 body{{font:14px/1.55 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
      color:#23201a;background:#faf9f6;margin:0;padding:28px}}
 .wrap{{max-width:760px;margin:0 auto}}
 h1{{font-size:21px;margin:0 0 4px}} h2{{font-size:15px;margin:30px 0 8px}}
 .sub{{color:#6c665c;font-size:13px;margin-bottom:22px}}
 table{{border-collapse:collapse;width:100%;margin:6px 0 4px}}
 th,td{{text-align:left;padding:7px 10px;border-bottom:1px solid #e6e1d6}}
 th{{font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:#6c665c}}
 td.n{{text-align:right;font-variant-numeric:tabular-nums}}
 .alert{{background:#fdf6e3;border-left:3px solid #c9a227;padding:12px 16px;margin:18px 0}}
 .alert ul{{margin:8px 0 0;padding-left:18px}}
 .note{{color:#6c665c;font-size:12.5px;margin-top:6px}}
 img{{max-width:100%;margin:10px 0}}
 footer{{color:#8a8377;font-size:12px;margin-top:34px;border-top:1px solid #e6e1d6;padding-top:12px}}
</style></head><body><div class=wrap>
<h1>Pulse and Glow — weekly funnel report</h1>
<div class=sub>This week {iso(this_start)} → {iso(end)} · prior week {iso(last_start)} → {iso(last_end)}<br>
Generated {datetime.now():%Y-%m-%d %H:%M} by <code>src/weekly_report.py</code></div>
{alert_html}
<h2>The funnel</h2>
<table><tr><th>Stage</th><th class=n>This week</th><th class=n>Prior week</th><th class=n>Change</th></tr>{rows}</table>
<p class=note>These four count <em>sessions in which the event occurred</em>, independently —
they are not a nested cohort, so dividing one stage by another is not a conversion rate
and can exceed 100%.</p>
<h2>Who the traffic actually was</h2>
<table><tr><th></th><th class=n>This week</th><th class=n>Prior week</th><th class=n>Change</th></tr>{who}</table>
<p class=note>Bot share this week: <b>{bot_share * 100:.1f}%</b> ·
Human cart-add rate: <b>{pct(now_b['human_carts'], now_b['human'])}</b></p>
<img src="data:image/png;base64,{chart}" alt="Weekly sessions, human vs total">
<h2>Entry page, humans only</h2>
<table><tr><th>Entry</th><th class=n>Sessions</th><th class=n>Cart adds</th><th class=n>Rate</th></tr>{entries}</table>
<p class=note>The headline finding, refreshed each week: homepage entrants add to cart,
product-page entrants historically do not.</p>
<footer>{stored} rows in funnel.db · pull and report both run from <code>src/</code>, no manual step</footer>
</div></body></html>"""

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    return subject, html, REPORT_DIR / f"{iso(end)}.html"


def send_email(subject: str, html: str) -> bool:
    """Email the report. Missing credentials is a skip, not a crash."""
    import smtplib
    from email.message import EmailMessage

    host, port = os.getenv("SMTP_HOST"), os.getenv("SMTP_PORT")
    user, password = os.getenv("SMTP_USER"), os.getenv("SMTP_PASSWORD")
    sender, recipient = os.getenv("REPORT_FROM"), os.getenv("REPORT_TO")

    if not all([host, port, user, password, sender, recipient]):
        print("email SKIPPED — SMTP settings incomplete in .env")
        return False

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = recipient
    message.set_content("This report is HTML. Open it in a mail client that renders HTML.")
    message.add_alternative(html, subtype="html")

    with smtplib.SMTP(host, int(port), timeout=30) as server:
        server.starttls()
        server.login(user, password)
        server.send_message(message)
    print(f"email sent to {recipient}")
    return True


def main() -> int:
    print(f"=== weekly run {datetime.now():%Y-%m-%d %H:%M:%S} ===")
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import pull_sessions
        pull_sessions.main()

        connection = sqlite3.connect(DB_PATH)
        subject, html, path = render(connection)
        connection.close()

        path.write_text(html)
        print(f"report written to {path.relative_to(PROJECT_ROOT)}")
        send_email(subject, html)
        print("=== run OK ===")
        return 0
    except Exception:
        # Loud on purpose. A scheduled job that fails quietly is worse than none.
        print("=== WEEKLY RUN FAILED ===", file=sys.stderr)
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
