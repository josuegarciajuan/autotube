CREATE TABLE IF NOT EXISTS account_upload_reservations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account TEXT NOT NULL,
    date_key TEXT NOT NULL,
    content_key TEXT NOT NULL,
    claimant TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'reserved'
        CHECK(state IN ('reserved', 'consumed')),
    reserved_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at TEXT NOT NULL,
    UNIQUE(account, date_key, content_key)
);

CREATE INDEX IF NOT EXISTS idx_account_upload_reservations_day
    ON account_upload_reservations(account, date_key, state);
