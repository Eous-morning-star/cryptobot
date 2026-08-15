-- tokenscore schema
-- Design principle: store EVERYTHING evaluated, including rejects.
-- You cannot tune a scorer without negative examples.

CREATE TABLE IF NOT EXISTS tokens (
    address         TEXT PRIMARY KEY,
    chain           TEXT NOT NULL DEFAULT 'solana',
    symbol          TEXT,
    name            TEXT,
    launchpad       TEXT,              -- pumpfun, bags, raydium, unknown
    deployer        TEXT,
    deployed_at     TIMESTAMPTZ,
    first_seen_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    metadata        JSONB DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_tokens_deployed  ON tokens (deployed_at DESC);
CREATE INDEX IF NOT EXISTS idx_tokens_deployer  ON tokens (deployer);

-- One row per source per token per fetch. Never overwrite: history is signal.
CREATE TABLE IF NOT EXISTS source_results (
    id              BIGSERIAL PRIMARY KEY,
    token_address   TEXT NOT NULL REFERENCES tokens(address) ON DELETE CASCADE,
    source          TEXT NOT NULL,     -- dexscreener, rugcheck, goplus, birdeye...
    fetched_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    ok              BOOLEAN NOT NULL,  -- false = source errored/timed out
    latency_ms      INTEGER,
    subscore        REAL,              -- 0..1 normalized, NULL if source only gates
    confidence      REAL,              -- 0..1, how much this source claims to know
    gate_failures   TEXT[] DEFAULT '{}',
    flags           TEXT[] DEFAULT '{}',
    raw             JSONB              -- full payload, for re-scoring old data later
);

CREATE INDEX IF NOT EXISTS idx_sr_token   ON source_results (token_address, fetched_at DESC);
CREATE INDEX IF NOT EXISTS idx_sr_source  ON source_results (source, fetched_at DESC);

-- Final verdict per evaluation run.
CREATE TABLE IF NOT EXISTS scores (
    id              BIGSERIAL PRIMARY KEY,
    token_address   TEXT NOT NULL REFERENCES tokens(address) ON DELETE CASCADE,
    scored_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    verdict         TEXT NOT NULL,     -- GATED | SCORED
    score           REAL,              -- 0..100, NULL when GATED
    confidence      REAL,              -- drops when sources are missing/disagree
    gate_failures   TEXT[] DEFAULT '{}',
    disagreements   TEXT[] DEFAULT '{}',
    sources_ok      INTEGER,
    sources_total   INTEGER,
    breakdown       JSONB,             -- per-source contribution, for the UI
    alerted         BOOLEAN NOT NULL DEFAULT false
);

CREATE INDEX IF NOT EXISTS idx_scores_token ON scores (token_address, scored_at DESC);
CREATE INDEX IF NOT EXISTS idx_scores_rank  ON scores (scored_at DESC, score DESC);

-- Social/narrative mentions from Reddit + Telethon.
CREATE TABLE IF NOT EXISTS mentions (
    id              BIGSERIAL PRIMARY KEY,
    platform        TEXT NOT NULL,     -- reddit, telegram
    channel         TEXT NOT NULL,
    external_id     TEXT NOT NULL,
    author          TEXT,
    posted_at       TIMESTAMPTZ NOT NULL,
    collected_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    body            TEXT,
    terms           TEXT[] DEFAULT '{}',   -- extracted n-grams
    addresses       TEXT[] DEFAULT '{}',   -- contract addresses found in text
    UNIQUE (platform, channel, external_id)
);

CREATE INDEX IF NOT EXISTS idx_mentions_time  ON mentions (posted_at DESC);
CREATE INDEX IF NOT EXISTS idx_mentions_terms ON mentions USING GIN (terms);
CREATE INDEX IF NOT EXISTS idx_mentions_addr  ON mentions USING GIN (addresses);

-- Rolling baseline per term, so you alert on acceleration not volume.
CREATE TABLE IF NOT EXISTS term_stats (
    term            TEXT PRIMARY KEY,
    bucket_counts   INTEGER[] DEFAULT '{}',  -- last N 15-min buckets
    mean_rate       REAL DEFAULT 0,
    stddev_rate     REAL DEFAULT 0,
    last_updated    TIMESTAMPTZ NOT NULL DEFAULT now(),
    peak_zscore     REAL DEFAULT 0
);

-- Wallets you track for early-buyer clustering.
CREATE TABLE IF NOT EXISTS tracked_wallets (
    address         TEXT PRIMARY KEY,
    label           TEXT,
    tier            INTEGER DEFAULT 3,   -- 1 = strongest historical signal
    wins            INTEGER DEFAULT 0,
    losses          INTEGER DEFAULT 0,
    added_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_active_at  TIMESTAMPTZ
);

-- Who bought a token early, and in what order.
CREATE TABLE IF NOT EXISTS early_buys (
    id              BIGSERIAL PRIMARY KEY,
    token_address   TEXT NOT NULL REFERENCES tokens(address) ON DELETE CASCADE,
    wallet          TEXT NOT NULL,
    buy_rank        INTEGER NOT NULL,    -- 1 = first buyer after deploy
    bought_at       TIMESTAMPTZ NOT NULL,
    sol_amount      NUMERIC(20, 9),
    signature       TEXT,
    UNIQUE (token_address, wallet, buy_rank)
);

CREATE INDEX IF NOT EXISTS idx_eb_wallet ON early_buys (wallet, bought_at DESC);
CREATE INDEX IF NOT EXISTS idx_eb_token  ON early_buys (token_address, buy_rank);

-- Ground truth. You label these later; without it you can never measure accuracy.
CREATE TABLE IF NOT EXISTS outcomes (
    token_address   TEXT PRIMARY KEY REFERENCES tokens(address) ON DELETE CASCADE,
    labelled_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    label           TEXT NOT NULL,       -- rug | dead | flat | winner
    peak_mcap_usd   NUMERIC(20, 2),
    mcap_now_usd    NUMERIC(20, 2),
    notes           TEXT
);
