-- ── llm_credit_status: track credit/balance state for LLM providers ──
CREATE TABLE IF NOT EXISTS llm_credit_status (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    provider      TEXT NOT NULL UNIQUE,     -- deepseek / openai / youtube
    status        TEXT NOT NULL DEFAULT 'unknown',  -- healthy / low / exhausted / error
    balance_usd   REAL,                     -- current balance in USD (DeepSeek only)
    error_count_7d INTEGER DEFAULT 0,       -- quota errors in last 7 days (OpenAI)
    last_error    TEXT,                     -- most recent error message (OpenAI)
    metadata_json TEXT,                     -- extra: currency, granted_balance, topped_up_balance, etc.
    checked_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_llm_credit_provider ON llm_credit_status(provider);
