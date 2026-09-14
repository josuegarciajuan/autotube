-- Autotube v58 schema: consumed_topics — registro anti-repetición compartido
-- entre long-forms y shorts, por canal.
--
-- Cada vez que se elige una temática (guion de vídeo largo o short nativo) se
-- registra aquí como "consumida" de forma permanente. La selección de nuevos
-- candidatos descarta cualquier tema con solapamiento de palabras clave por
-- encima del umbral configurable (TOPIC_DEDUP_THRESHOLD).
--
-- Ver pipeline/topic_dedup.py para la normalización y la métrica de similitud.

CREATE TABLE IF NOT EXISTS consumed_topics (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id  INTEGER NOT NULL,
    topic_label TEXT    NOT NULL,              -- etiqueta legible (título/tema)
    topic_norm  TEXT    NOT NULL,              -- normalizada (clave de unicidad)
    tokens_json TEXT    NOT NULL,              -- JSON array de tokens significativos
    source      TEXT    NOT NULL,              -- longform|native_short|standalone_short|clip
    ref_id      INTEGER,                       -- id de videos.id / shorts.id
    created_at  TEXT    DEFAULT (datetime('now')),
    UNIQUE(channel_id, topic_norm)
);

CREATE INDEX IF NOT EXISTS idx_consumed_topics_channel
    ON consumed_topics(channel_id, created_at);
