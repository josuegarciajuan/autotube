"""Database connection and helpers for Autotube.
 
Provides a context manager for SQLite connections and convenience
functions for CRUD operations on raw_content, scripts, and videos.
"""
 
import json
import sqlite3
import os
import time
import functools
import logging
from pathlib import Path
 
from config.settings import DATABASE_PATH

_log = logging.getLogger("autotube.db")

# ── Retry decorator for SQLite lock contention ──────────────
# Wraps DB operations that can fail with "database is locked".
# Uses exponential backoff: 0.5s → 1s → 2s → 4s → 8s (max 5 retries).
# This prevents single-lock events from cascading into unrecoverable
# failures where even the error handler can't write to the DB.

DB_LOCK_RETRIES = int(os.getenv("DB_LOCK_RETRIES", "5"))
DB_LOCK_BACKOFF_BASE = float(os.getenv("DB_LOCK_BACKOFF_BASE", "0.5"))


def _with_db_lock_retry(func):
    """Decorator: retry on sqlite3.OperationalError 'database is locked'."""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        last_exc = None
        for attempt in range(DB_LOCK_RETRIES + 1):
            try:
                return func(*args, **kwargs)
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() and "database is locked" not in str(exc).lower():
                    raise
                last_exc = exc
                if attempt < DB_LOCK_RETRIES:
                    wait = DB_LOCK_BACKOFF_BASE * (2 ** attempt)
                    _log.warning(
                        "DB locked (attempt %d/%d), retrying in %.1fs...",
                        attempt + 1, DB_LOCK_RETRIES, wait,
                    )
                    time.sleep(wait)
                else:
                    _log.error(
                        "DB locked after %d retries — giving up: %s",
                        DB_LOCK_RETRIES, exc,
                    )
        raise last_exc
    return wrapper


SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def init_db(db_path: str = None) -> sqlite3.Connection:
    """Initialize the database with schema if not exists."""
    if db_path is None:
        db_path = str(DATABASE_PATH)
    conn = sqlite3.connect(db_path, timeout=60)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=45000")
    conn.execute("PRAGMA wal_autocheckpoint=100")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    if SCHEMA_PATH.exists():
        with open(SCHEMA_PATH) as f:
            conn.executescript(f.read())
    conn.commit()
    return conn


class Database:
    """Thin wrapper around SQLite for the Autotube pipeline."""

    def __init__(self, db_path: str = None):
        self.db_path = db_path or str(DATABASE_PATH)
        if not os.path.exists(self.db_path):
            init_db(self.db_path)

    def _connect(self) -> sqlite3.Connection:
        """Return a SQLite connection with performance PRAGMAs.

        Uses WAL mode for concurrent reads, aggressive caching, and memory-mapped
        I/O to reduce filesystem overhead on read-heavy dashboard workloads.
        """
        conn = sqlite3.connect(self.db_path, timeout=60)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")  # concurrent read/write
        conn.execute("PRAGMA busy_timeout=45000")  # 45s wait on lock
        conn.execute("PRAGMA wal_autocheckpoint=100")  # keep WAL small
        # ── Performance PRAGMAs (v1.0 — connection reuse optimization) ──
        conn.execute("PRAGMA cache_size=-64000")  # 64MB page cache (negative = KB)
        conn.execute("PRAGMA mmap_size=134217728")  # 128MB memory-mapped I/O
        conn.execute("PRAGMA synchronous=NORMAL")  # faster writes, WAL-safe
        conn.execute("PRAGMA temp_store=MEMORY")  # temp tables in RAM
        conn.execute("PRAGMA optimize")  # run ANALYZE hints on close
        return conn

    @_with_db_lock_retry
    def _execute_write(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        """Execute a write (INSERT/UPDATE/DELETE) with DB lock retry support."""
        with self._connect() as conn:
            cursor = conn.execute(sql, params)
            conn.commit()
            return cursor

    @staticmethod
    def _resolve_canal(canal: str | None = None) -> str:
        """Resolve canal slug from explicit parameter or fallback to first active channel."""
        if canal:
            return canal
        from config.settings import ACTIVE_CHANNELS
        return ACTIVE_CHANNELS[0] if ACTIVE_CHANNELS else "unknown"

    # ── raw_content ────────────────────────────────────────────

    @_with_db_lock_retry
    def insert_raw_content(self, source, url, title, text,
                           subreddit=None, score=0, canal=None):
        """Insert scraped content. Returns row id. Skips duplicates by URL."""
        canal = self._resolve_canal(canal)
        with self._connect() as conn:
            try:
                cursor = conn.execute(
                    """INSERT INTO raw_content
                       (source, subreddit, url, title, text, score, canal)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (source, subreddit, url, title, text, score, canal),
                )
                conn.commit()
                return cursor.lastrowid
            except sqlite3.IntegrityError:
                return None

    @_with_db_lock_retry
    def insert_raw_content_viral(self, source, url, title, text,
                                  subreddit=None, score=0, canal=None,
                                  source_mode="viral",
                                  viral_original_title=None,
                                  viral_original_description=None,
                                  viral_original_thumbnail_url=None,
                                  viral_original_video_url=None,
                                  viral_views=0,
                                  viral_upload_date=None,
                                  viral_duration_sec=0,
                                  viral_channel_name=None,
                                  viral_score=0.0,
                                  viral_script_es=None,
                                  viral_meta_json=None):
        """Insert or update viral content. Returns row id on success, None on error.
        
        If the URL already exists for this canal, the viral metadata columns
        (script, score, views, etc.) are updated in-place. The row id of the
        existing record is returned.
        
        Previously this method silently swallowed IntegrityError (returning None)
        when the same URL existed for another canal — causing the caller to
        fall back to id=0 which breaks the scripts.raw_content_id FK constraint.
        """
        canal = self._resolve_canal(canal)
        with self._connect() as conn:
            try:
                # Attempt INSERT first
                cursor = conn.execute(
                    """INSERT INTO raw_content
                       (source, subreddit, url, title, text, score, canal,
                        source_mode, viral_original_title, viral_original_description,
                        viral_original_thumbnail_url, viral_original_video_url,
                        viral_views, viral_upload_date, viral_duration_sec,
                        viral_channel_name, viral_score, viral_script_es, viral_meta_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (source, subreddit, url, title, text, score, canal,
                     source_mode, viral_original_title, viral_original_description,
                     viral_original_thumbnail_url, viral_original_video_url,
                     viral_views, viral_upload_date, viral_duration_sec,
                     viral_channel_name, viral_score, viral_script_es, viral_meta_json),
                )
                conn.commit()
                _log.debug("insert_raw_content_viral: new row id=%s for url=%s canal=%s",
                           cursor.lastrowid, url, canal)
                return cursor.lastrowid
            except sqlite3.IntegrityError:
                # URL already exists for this canal → UPDATE the viral fields
                # (unless it's from a DIFFERENT canal — then we still do INSERT via the new UNIQUE(url, canal))
                _log.info("insert_raw_content_viral: url already exists for canal=%s → updating viral fields (url=%s)",
                          canal, url)
                try:
                    update_cursor = conn.execute(
                        """UPDATE raw_content SET
                            source = COALESCE(?, source),
                            subreddit = COALESCE(?, subreddit),
                            title = COALESCE(?, title),
                            text = COALESCE(?, text),
                            score = COALESCE(?, score),
                            source_mode = COALESCE(?, source_mode),
                            viral_original_title = COALESCE(?, viral_original_title),
                            viral_original_description = COALESCE(?, viral_original_description),
                            viral_original_thumbnail_url = COALESCE(?, viral_original_thumbnail_url),
                            viral_original_video_url = COALESCE(?, viral_original_video_url),
                            viral_views = COALESCE(?, viral_views),
                            viral_upload_date = COALESCE(?, viral_upload_date),
                            viral_duration_sec = COALESCE(?, viral_duration_sec),
                            viral_channel_name = COALESCE(?, viral_channel_name),
                            viral_score = COALESCE(?, viral_score),
                            viral_script_es = COALESCE(?, viral_script_es),
                            viral_meta_json = COALESCE(?, viral_meta_json)
                           WHERE url = ? AND canal = ?""",
                        (source, subreddit, title, text, score, source_mode,
                         viral_original_title, viral_original_description,
                         viral_original_thumbnail_url, viral_original_video_url,
                         viral_views, viral_upload_date, viral_duration_sec,
                         viral_channel_name, viral_score, viral_script_es, viral_meta_json,
                         url, canal),
                    )
                    conn.commit()
                    # Fetch the row id that was just updated
                    row = conn.execute(
                        "SELECT id FROM raw_content WHERE url = ? AND canal = ?",
                        (url, canal),
                    ).fetchone()
                    if row:
                        _log.info("insert_raw_content_viral: updated existing row id=%s for url=%s canal=%s",
                                  row["id"], url, canal)
                        return row["id"]
                    else:
                        _log.error("insert_raw_content_viral: UPDATE succeeded but row not found for url=%s canal=%s",
                                   url, canal)
                        return None
                except sqlite3.IntegrityError as exc2:
                    _log.error("insert_raw_content_viral: UPDATE also failed for url=%s canal=%s: %s",
                               url, canal, exc2)
                    return None

    def get_viral_candidates(self, canal=None, min_score=0.0, limit=20):
        """Fetch viral candidates ordered by viral_score (highest first)."""
        canal = self._resolve_canal(canal)
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM raw_content
                   WHERE canal = ? AND source_mode = 'viral' AND used = 0 AND viral_score >= ?
                   ORDER BY viral_score DESC, scraped_at DESC
                   LIMIT ?""",
                (canal, min_score, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_content_by_url(self, url, canal=None):
        """Check if a URL already exists in raw_content for deduplication."""
        canal = self._resolve_canal(canal)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM raw_content WHERE url = ? AND canal = ?",
                (url, canal),
            ).fetchone()
        return dict(row) if row else None

    def get_content_by_id(self, content_id: int):
        """Get a raw_content row by id."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM raw_content WHERE id = ?",
                (content_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_unused_content(self, canal=None, limit=10, strategy: str = "best_first"):
        """Fetch unused scraped content.

        Args:
            canal: Channel slug.
            limit: Max items to return.
            strategy: Content ordering strategy:
                - "best_first": Longest text + highest score first (default).
                - "newest_first": Most recently scraped first.
                - "oldest_first": Oldest first (legacy).
                - "highest_score": Highest Reddit score first.
        """
        canal = self._resolve_canal(canal)
        if strategy == "newest_first":
            order_clause = "ORDER BY scraped_at DESC"
        elif strategy == "oldest_first":
            order_clause = "ORDER BY scraped_at ASC"
        elif strategy == "highest_score":
            order_clause = "ORDER BY score DESC, LENGTH(text) DESC"
        else:  # best_first (default)
            order_clause = "ORDER BY LENGTH(text) DESC, score DESC"

        with self._connect() as conn:
            rows = conn.execute(
                f"""SELECT * FROM raw_content
                    WHERE canal = ? AND used = 0
                    {order_clause} LIMIT ?""",
                (canal, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    @_with_db_lock_retry
    def mark_content_used(self, content_id):
        """Mark scraped content as processed."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE raw_content SET used = 1 WHERE id = ?",
                (content_id,),
            )
            conn.commit()

    def get_unused_count(self, canal=None):
        """Return count of unused content items."""
        canal = self._resolve_canal(canal)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM raw_content WHERE canal = ? AND used = 0",
                (canal,),
            ).fetchone()
        return row["cnt"] if row else 0

    # ── scripts ────────────────────────────────────────────────

    @_with_db_lock_retry
    def insert_script(self, raw_content_id, canal, titulo_options,
                      guion, escenas, bloques=None, emociones=None, keywords=None,
                      duracion_estimada=None, token_count=0, cost_estimate=0.0,
                      emergency_mode=False):
        """Insert a generated script. Returns row id."""
        titulo_selected = titulo_options[0] if titulo_options else ""
        with self._connect() as conn:
            cursor = conn.execute(
                """INSERT INTO scripts
                   (raw_content_id, canal, titulo_options, titulo_selected, guion,
                    escenas_json, bloques_json, emociones_json, keywords_json,
                    duracion_estimada, token_count, cost_estimate, emergency_mode)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    raw_content_id, canal,
                    json.dumps(titulo_options, ensure_ascii=False),
                    titulo_selected,
                    guion,
                    json.dumps(escenas, ensure_ascii=False),
                    json.dumps(bloques, ensure_ascii=False) if bloques else None,
                    json.dumps(emociones or [], ensure_ascii=False),
                    json.dumps(keywords or [], ensure_ascii=False),
                    duracion_estimada, token_count, cost_estimate,
                    1 if emergency_mode else 0,
                ),
            )
            conn.commit()
            return cursor.lastrowid

    def get_unused_scripts(self, canal=None, limit=5):
        """Fetch unused scripts sorted by creation time."""
        canal = self._resolve_canal(canal)
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM scripts
                   WHERE canal = ? AND used = 0
                   ORDER BY created_at ASC LIMIT ?""",
                (canal, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    @_with_db_lock_retry
    def mark_script_used(self, script_id):
        """Mark a script as processed."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE scripts SET used = 1 WHERE id = ?",
                (script_id,),
            )
            conn.commit()

    def get_script(self, script_id):
        """Fetch a single script by ID."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM scripts WHERE id = ?",
                (script_id,),
            ).fetchone()
        return dict(row) if row else None

    # ── videos ─────────────────────────────────────────────────

    @_with_db_lock_retry
    def insert_video(self, script_id, canal, video_path, thumbnail_path=None,
                     audio_path=None, titulo_final=None, duracion_seg=None,
                     privacy_status="public", channel_id=None,
                     description=None, tags_json=None, title_options=None,
                     timing_data=None, source_url=None, source_mode=None):
        """Insert a video record. Returns row id.
        
        Args:
            channel_id: Optional channels.id to link video to a channel (v2).
            description: Optional YouTube description text.
            tags_json: Optional JSON string of tags array.
            title_options: Optional JSON string of title options array.
            source_url: Optional original source URL (viral mirror).
            source_mode: Optional source mode ('original' or 'viral').
        """
        with self._connect() as conn:
            # Build dynamic INSERT to handle optional v2 columns
            columns = ["script_id", "canal", "video_path", "thumbnail_path",
                       "audio_path", "titulo_final", "duracion_seg", "privacy_status"]
            values = [script_id, canal, video_path, thumbnail_path,
                      audio_path, titulo_final, duracion_seg, privacy_status]
            
            if channel_id is not None:
                columns.append("channel_id")
                values.append(channel_id)
            if description is not None:
                columns.append("description")
                values.append(description)
            if tags_json is not None:
                columns.append("tags_json")
                values.append(tags_json)
            if title_options is not None:
                columns.append("title_options")
                values.append(title_options)
            if timing_data is not None:
                columns.append("timing_data")
                values.append(timing_data)
            if source_url is not None:
                columns.append("source_url")
                values.append(source_url)
            if source_mode is not None:
                columns.append("source_mode")
                values.append(source_mode)
            
            placeholders = ", ".join(["?"] * len(values))
            cols_str = ", ".join(columns)
            
            cursor = conn.execute(
                f"INSERT INTO videos ({cols_str}) VALUES ({placeholders})",
                values,
            )
            conn.commit()
            return cursor.lastrowid

    @_with_db_lock_retry
    def mark_video_uploaded(self, video_id, yt_video_id, yt_url):
        """Record YouTube upload results."""
        with self._connect() as conn:
            conn.execute(
                """UPDATE videos
                   SET yt_video_id = ?, yt_url = ?, uploaded_at = CURRENT_TIMESTAMP
                   WHERE id = ?""",
                (yt_video_id, yt_url, video_id),
            )
            conn.commit()

    def get_unuploaded_videos(self, canal=None, limit=5):
        """Fetch videos not yet uploaded to YouTube."""
        canal = self._resolve_canal(canal)
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM videos
                   WHERE canal = ? AND yt_video_id IS NULL
                   ORDER BY created_at ASC LIMIT ?""",
                (canal, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_videos_today(self, canal=None):
        """Count videos uploaded today for a channel."""
        canal = self._resolve_canal(canal)
        with self._connect() as conn:
            row = conn.execute(
                """SELECT COUNT(*) as cnt FROM videos
                   WHERE canal = ?
                   AND DATE(uploaded_at) = DATE('now')
                   AND yt_video_id IS NOT NULL""",
                (canal,),
            ).fetchone()
        return row["cnt"] if row else 0

    # ── pipeline_log ───────────────────────────────────────────

    @_with_db_lock_retry
    def log_pipeline(self, canal, phase, status, message=None,
                     content_id=None, duration_ms=None):
        """Log a pipeline execution event."""
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO pipeline_log
                   (canal, phase, status, message, content_id, duration_ms)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (canal, phase, status, message, content_id, duration_ms),
            )
            conn.commit()
