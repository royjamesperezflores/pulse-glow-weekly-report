-- Funnel metrics, stored long: one row per date x dimension x value.
-- date holds 'YYYY-MM-DD' for day-level rows, or 'YYYY-MM-DD..YYYY-MM-DD'
-- when ShopifyQL would not break that dimension down by day.
-- dimension 'day' with value 'total' is the site-wide daily total.

CREATE TABLE IF NOT EXISTS session_metrics_daily (
  date TEXT NOT NULL,
  dimension TEXT NOT NULL,
  dimension_value TEXT NOT NULL,
  sessions INTEGER NOT NULL,
  cart_add_sessions INTEGER NOT NULL,
  checkout_sessions INTEGER NOT NULL,
  completed_sessions INTEGER NOT NULL,
  PRIMARY KEY (date, dimension, dimension_value)
);
