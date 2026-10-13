-- Key/value metadata (e.g. date of the last scrip master download).
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Cached subset of Dhan's scrip master (equity + index rows).
CREATE TABLE IF NOT EXISTS instruments (
    exchange         TEXT NOT NULL,   -- NSE / BSE
    exchange_segment TEXT NOT NULL,   -- NSE_EQ / BSE_EQ / IDX_I
    security_id      TEXT NOT NULL,
    trading_symbol   TEXT NOT NULL,
    instrument       TEXT NOT NULL,   -- EQUITY / INDEX
    series           TEXT NOT NULL DEFAULT '',
    lot_size         INTEGER NOT NULL DEFAULT 1,
    tick_size        REAL NOT NULL DEFAULT 0,
    name             TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (exchange_segment, security_id)
);

CREATE INDEX IF NOT EXISTS idx_instruments_symbol ON instruments (exchange, trading_symbol);
