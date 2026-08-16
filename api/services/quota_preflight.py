"""Offline backlog preflight; deliberately makes no YouTube API calls."""

from __future__ import annotations

import sqlite3
from collections import defaultdict


LONG_UPLOADS_PER_PROJECT = 3
SHORT_UPLOADS_PER_PROJECT = 2
UPLOAD_UNITS = 1600


def build_project_preflight(db_path: str, channel_projects: dict[str, str]) -> dict:
    """Return the deterministic upload plan used for operator approval.

    The current remediation phase only exposes this data. It never schedules,
    uploads, mutates rows, or authenticates against YouTube.
    """
    projects: dict[str, dict] = defaultdict(lambda: {
        "channels": [],
        "eligible_long_video_ids": [],
        "uploaded_private_video_ids": [],
        "long_upload_capacity": LONG_UPLOADS_PER_PROJECT,
        "short_upload_capacity": SHORT_UPLOADS_PER_PROJECT,
        "automatic_upload_capacity": LONG_UPLOADS_PER_PROJECT + SHORT_UPLOADS_PER_PROJECT,
        "automatic_units": (LONG_UPLOADS_PER_PROJECT + SHORT_UPLOADS_PER_PROJECT) * UPLOAD_UNITS,
        "reserved_essential_units": 2000,
    })
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        channel_rows = conn.execute(
            "SELECT id, slug FROM channels WHERE active = 1 ORDER BY id"
        ).fetchall()
        channel_by_id = {row["id"]: row["slug"] for row in channel_rows}
        for row in channel_rows:
            project = channel_projects.get(row["slug"], "unknown")
            projects[project]["channels"].append(row["slug"])

        rows = conn.execute(
            """SELECT id, channel_id, status, created_at
               FROM videos
               WHERE status IN ('awaiting_upload', 'uploaded_private')
               ORDER BY CASE status WHEN 'awaiting_upload' THEN 0 ELSE 1 END,
                        created_at ASC, id ASC"""
        ).fetchall()
        for row in rows:
            slug = channel_by_id.get(row["channel_id"])
            if not slug:
                continue
            project = channel_projects.get(slug, "unknown")
            key = "eligible_long_video_ids" if row["status"] == "awaiting_upload" else "uploaded_private_video_ids"
            projects[project][key].append(row["id"])
    finally:
        conn.close()

    return {
        "mode": "simulation",
        "youtube_calls": 0,
        "projects": dict(projects),
    }
