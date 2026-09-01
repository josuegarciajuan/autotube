CREATE TABLE IF NOT EXISTS delivery_profiles (
    state TEXT PRIMARY KEY CHECK(state IN ('strike','recovery','normal')),
    public_videos_per_day INTEGER NOT NULL CHECK(public_videos_per_day >= 0),
    native_shorts_per_day INTEGER NOT NULL DEFAULT 1,
    global_shorts_per_day INTEGER NOT NULL DEFAULT 6,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
INSERT OR IGNORE INTO delivery_profiles(state, public_videos_per_day, native_shorts_per_day, global_shorts_per_day)
VALUES ('strike', 1, 1, 6), ('recovery', 1, 2, 8), ('normal', 2, 3, 12);
