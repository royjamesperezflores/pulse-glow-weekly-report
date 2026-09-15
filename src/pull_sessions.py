"""Pull 90 days of Shopify session funnel metrics and load them into SQLite.

One run does three things:
  1. trade client credentials for a fresh Admin API token (shopify_client.py)
  2. ask ShopifyQL for the four funnel metrics, once per dimension
  3. upsert every row into data/funnel.db

ShopifyQL is not SQL. Its metrics are already aggregates -- SHOW names which
totals you want, there is no sum() to apply.

The GraphQL shape below was validated against the live 2025-10 Admin schema:
  shopifyqlQuery -> ShopifyqlQueryResponse { parseErrors: [String!]!
                                             tableData { columns, rows } }
Only scope required: read_reports.
"""
import json
import os
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

from shopify_client import get_access_token

load_dotenv()

STORE = os.getenv("SHOPIFY_STORE")
API_VERSION = "2025-10"  # shopifyqlQuery requires 2025-10 or higher
URL = f"https://{STORE}.myshopify.com/admin/api/{API_VERSION}/graphql.json"

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "data" / "funnel.db"
SCHEMA_PATH = PROJECT_ROOT / "sql" / "schema.sql"

DAYS = 90
WINDOW_END = date.today()
WINDOW_START = WINDOW_END - timedelta(days=DAYS)
WINDOW_LABEL = f"{WINDOW_START.isoformat()}..{WINDOW_END.isoformat()}"

# The four funnel stages, in ShopifyQL's exact spelling.
METRICS = [
    "sessions",
    "sessions_with_cart_additions",
    "sessions_that_reached_checkout",
    "sessions_that_completed_checkout",
]

# Maps a ShopifyQL metric name to its column in session_metrics_daily.
METRIC_COLUMNS = {
    "sessions": "sessions",
    "sessions_with_cart_additions": "cart_add_sessions",
    "sessions_that_reached_checkout": "checkout_sessions",
    "sessions_that_completed_checkout": "completed_sessions",
}

# Dimensions verified live against this store on 2026-09-09. These spellings
# are not guessable -- session_landing_page and landing_page both 404.
DIMENSIONS = [
    ["landing_page_type"],
    ["landing_page_path"],
    ["session_device_type"],
    ["referrer_source"],
    ["human_or_bot_session"],
    # The confound test for Finding 1: if product-page entrants skew bot and
    # homepage entrants skew human, part of the cart-add spread is robots.
    ["landing_page_type", "human_or_bot_session"],
]

GRAPHQL = """
query RunShopifyQL($query: String!) {
  shopifyqlQuery(query: $query) {
    parseErrors
    tableData {
      columns { name dataType }
      rows
    }
  }
}
"""


def run_shopifyql(token: str, query: str) -> dict | None:
    """Send one ShopifyQL string. Return tableData, or None if it was rejected."""
    response = requests.post(
        URL,
        headers={"X-Shopify-Access-Token": token, "Content-Type": "application/json"},
        json={"query": GRAPHQL, "variables": {"query": query}},
        timeout=60,
    )
    response.raise_for_status()  # turn a 4xx/5xx into a loud Python error
    payload = response.json()

    # A GraphQL server answers 200 even when the query itself is wrong,
    # so the errors key has to be checked by hand.
    if payload.get("errors"):
        print(f"    GraphQL error: {json.dumps(payload['errors'])[:400]}")
        return None

    result = (payload.get("data") or {}).get("shopifyqlQuery")
    if not result:
        print("    shopifyqlQuery returned null")
        return None

    for message in result.get("parseErrors") or []:
        print(f"    parse error: {message}")
    if result.get("parseErrors"):
        return None

    return result.get("tableData")


def build_query(dimensions: list[str] | None, timeseries: bool) -> str:
    """Compose one ShopifyQL statement."""
    parts = ["FROM sessions", f"SHOW {', '.join(METRICS)}"]
    if dimensions:
        # GROUP BY, not BY. ShopifyQL's parser rejects a bare BY outright.
        parts.append(f"GROUP BY {', '.join(dimensions)}")
    if timeseries:
        parts.append("TIMESERIES day")
    parts.append(f"SINCE -{DAYS}d UNTIL today")
    return " ".join(parts)


def to_iso_date(value) -> str:
    """ShopifyQL day columns come back as epoch seconds or as an ISO string."""
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc).date().isoformat()
    text = str(value)
    if text.replace(".", "", 1).isdigit():
        return datetime.fromtimestamp(float(text), tz=timezone.utc).date().isoformat()
    return text[:10]


def parse_rows(table_data: dict, dimensions: list[str] | None) -> list[tuple]:
    """Turn ShopifyQL's column/row shape into rows for session_metrics_daily."""
    columns = [c["name"] for c in table_data["columns"]]
    types = {c["name"]: c["dataType"] for c in table_data["columns"]}

    # Any *_TIMESTAMP column is the time axis; TIMESERIES day names it 'day'.
    date_col = next(
        (c for c in columns if c == "day" or types.get(c, "").endswith("TIMESTAMP")),
        None,
    )
    # Dimension columns are whatever is left after the date and the metrics.
    dim_cols = [c for c in columns if c not in METRIC_COLUMNS and c != date_col]

    rows = []
    for raw in table_data.get("rows") or []:
        # rows is a JSON scalar: a list of lists, or of dicts on some versions.
        record = raw if isinstance(raw, dict) else dict(zip(columns, raw))

        row_date = to_iso_date(record[date_col]) if date_col else WINDOW_LABEL
        if not dimensions:
            dim_name, dim_value = "day", "total"
        else:
            # A cross-tab is stored as "a+b" with values joined by " | ".
            dim_name = "+".join(dimensions)
            dim_value = " | ".join(str(record.get(c)) for c in dim_cols) or "unknown"

        def metric(name: str) -> int:
            value = record.get(name)
            return int(float(value)) if value not in (None, "") else 0

        rows.append((
            row_date,
            dim_name,
            dim_value,
            metric("sessions"),
            metric("sessions_with_cart_additions"),
            metric("sessions_that_reached_checkout"),
            metric("sessions_that_completed_checkout"),
        ))
    return rows


def upsert(connection: sqlite3.Connection, rows: list[tuple]) -> None:
    """Insert rows, overwriting any row with the same (date, dimension, value)."""
    connection.executemany(
        """
        INSERT INTO session_metrics_daily
            (date, dimension, dimension_value,
             sessions, cart_add_sessions, checkout_sessions, completed_sessions)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(date, dimension, dimension_value) DO UPDATE SET
            sessions           = excluded.sessions,
            cart_add_sessions  = excluded.cart_add_sessions,
            checkout_sessions  = excluded.checkout_sessions,
            completed_sessions = excluded.completed_sessions
        """,
        rows,
    )


def main() -> None:
    print(f"window         : {WINDOW_LABEL} ({DAYS} days)")
    token = get_access_token()

    DB_PATH.parent.mkdir(exist_ok=True)
    connection = sqlite3.connect(DB_PATH)
    connection.executescript(SCHEMA_PATH.read_text())

    # None means the daily total with no breakdown.
    jobs = [(None, "day (daily totals)")] + [(d, " x ".join(d)) for d in DIMENSIONS]
    total_rows = 0

    for dimensions, label in jobs:
        print(f"\npulling {label}")

        # Prefer day-level rows. Some dimensions refuse BY + TIMESERIES
        # together, so fall back to one aggregate row per dimension value.
        table_data = run_shopifyql(token, build_query(dimensions, timeseries=True))
        grain = "daily"
        if table_data is None and dimensions is not None:
            print("    retrying without TIMESERIES (90-day aggregate)")
            table_data = run_shopifyql(token, build_query(dimensions, timeseries=False))
            grain = "window aggregate"

        if table_data is None:
            print("    SKIPPED -- no data returned")
            continue

        rows = parse_rows(table_data, dimensions)
        if not rows:
            print("    0 rows returned")
            continue

        upsert(connection, rows)
        connection.commit()
        total_rows += len(rows)
        print(f"    {len(rows)} rows ({grain}), {sum(r[3] for r in rows)} sessions")

    stored = connection.execute("SELECT COUNT(*) FROM session_metrics_daily").fetchone()[0]
    connection.close()
    print(f"\nwrote {total_rows} rows this run; {stored} rows now in {DB_PATH.name}")


if __name__ == "__main__":
    main()
