# pulse-glow-weekly-report

Automated weekly analytics report for a live Shopify store. Refreshes 90 days of
session data, renders a charted HTML report comparing this week to last, emails
it, and logs run status so a silent failure surfaces instead of going unnoticed.

Python · ShopifyQL (Admin GraphQL API) · SQLite · matplotlib · SMTP · cron

This is the companion to [`pulse-glow-funnel-analysis`](https://github.com/royjamesperezflores/pulse-glow-funnel-analysis),
which established the findings. That project answered a question once. This one
answers it every week without being asked.

## What a run does

```
cron → scripts/run_weekly.sh → src/weekly_report.py
                                 ├─ src/shopify_client.py   fresh 24h token
                                 ├─ src/pull_sessions.py    7 ShopifyQL queries → SQLite
                                 ├─ render                  HTML + inline chart
                                 └─ send                    SMTP
                               all output → logs/scheduler.log, exit code preserved
```

The pull and the client are the same modules the funnel analysis uses, unchanged.

## Design decisions worth explaining

**The token is never cached.** Shopify's client-credentials grant returns a token
that expires in 86,399 seconds. A scheduler that stored one would work for a day
and then fail quietly forever, so every run exchanges credentials afresh.

**Re-running is harmless.** Rows upsert on `(date, dimension, dimension_value)`,
so a manual run, a retry, and the scheduled run on the same day converge on the
same table rather than duplicating it.

**It fails loudly.** Any exception is logged with a traceback and the process
exits non-zero, so cron's own failure mail fires and the log shows it. A missing
SMTP password is treated as a skip rather than a crash — the report is still
written — because losing the report to a mail problem would be the wrong tradeoff.

**Alerts are thresholds, not commentary.** Bot share at or above 80%, or fewer
than 20 human sessions in the week, puts a banner at the top. Both fired on the
first production run, which is the point: the numbers below them are not
measurable, and the report says so rather than presenting them as findings.

**The chart is a base64 data URI.** Inlined into the HTML so the email renders
with no attachment and no image hosting.

**The report refuses to divide the funnel stages.** The four metrics count
sessions in which an event occurred, independently — they are not a nested
cohort, and dividing one stage by another can exceed 100% in this data. The
report shows the stages and prints that caveat instead of a fake conversion rate.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env     # Shopify credentials + SMTP (Gmail needs an App Password)
./scripts/run_weekly.sh  # run it once by hand
tail -30 logs/scheduler.log
```

Schedule it — Mondays at 8am:

```bash
(crontab -l 2>/dev/null; echo "0 8 * * 1 $HOME/Developer/pulse-glow-weekly-report/scripts/run_weekly.sh") | crontab -
```

`scripts/run_weekly.sh` uses absolute paths throughout, because cron runs with a
bare environment and no shell profile.

| File | Does |
|---|---|
| `src/weekly_report.py` | Orchestrates the run, renders the HTML, sends the mail |
| `src/pull_sessions.py` | Seven ShopifyQL queries → upsert into `data/funnel.db` |
| `src/shopify_client.py` | Client-credentials grant → 24-hour Admin API token |
| `scripts/run_weekly.sh` | Cron entrypoint; logging and exit-code handling |
| `sql/schema.sql` | `session_metrics_daily` |

Reports, the database, logs and `.env` are gitignored — this repo is the pipeline,
not the data.
