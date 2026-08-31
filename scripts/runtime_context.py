"""Canonical runtime context and explicit channel selection for incident scripts.

This module deliberately resolves identity from the database and paths from
``config.settings``.  Scripts must not carry channel maps or production paths.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


class SelectorError(ValueError):
    """The operator supplied an unsafe or ambiguous channel selector."""


@dataclass(frozen=True)
class ChannelContext:
    id: int
    slug: str
    project: str
    google_account: str | None
    token_path: Path
    output_dir: Path


def runtime_paths() -> dict[str, Path]:
    """Return configured runtime paths without embedding deployment paths."""
    from config import settings

    return {
        "project_root": settings.PROJECT_ROOT,
        "database": Path(settings.DATABASE_PATH),
        "tokens": settings.TOKENS_DIR,
        "output": settings.OUTPUT_DIR,
        "thumbnails": settings.THUMBNAILS_DIR,
    }


def _project_for(slug: str) -> str:
    from api.services.quota_tracker import get_channel_project

    return get_channel_project(slug)


def resolve_channels(
    *,
    db_path: Path | str | None = None,
    channel_id: int | None = None,
    slug: str | None = None,
    project: str | None = None,
    all_channels: bool = False,
    yes: bool = False,
    active_only: bool = True,
    project_resolver: Callable[[str], str] | None = None,
) -> list[ChannelContext]:
    """Resolve exactly one explicit selector into deterministic channel contexts."""
    selectors = [channel_id is not None, bool(slug), bool(project), all_channels]
    if sum(selectors) != 1:
        raise SelectorError("indica exactamente un selector: --channel-id, --slug, --project o --all")
    if all_channels and not yes:
        raise SelectorError("--all requiere --yes como confirmación explícita")

    paths = runtime_paths()
    database = Path(db_path) if db_path is not None else paths["database"]
    if not database.exists():
        raise SelectorError(f"DB no encontrada: {database}; configura DATABASE_PATH")
    resolver = project_resolver or _project_for
    conn = sqlite3.connect(str(database))
    conn.row_factory = sqlite3.Row
    query = "SELECT id, slug, google_account FROM channels"
    clauses: list[str] = []
    params: list[object] = []
    if active_only:
        clauses.append("active = 1")
    if channel_id is not None:
        clauses.append("id = ?")
        params.append(channel_id)
    elif slug:
        clauses.append("slug = ?")
        params.append(slug)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY id"
    rows = conn.execute(query, params).fetchall()
    conn.close()

    if channel_id is not None or slug:
        if len(rows) != 1:
            requested = f"id={channel_id}" if channel_id is not None else f"slug={slug}"
            raise SelectorError(f"selector {requested} no resuelve exactamente un canal activo")
    if project:
        rows = [row for row in rows if resolver(row["slug"]) == project]
        if not rows:
            raise SelectorError(f"no hay canales activos para el proyecto {project!r}")

    return [
        ChannelContext(
            id=int(row["id"]),
            slug=row["slug"],
            project=resolver(row["slug"]),
            google_account=row["google_account"],
            token_path=paths["tokens"] / f"{row['slug']}.pickle",
            output_dir=paths["output"],
        )
        for row in rows
    ]


def add_channel_selector_arguments(parser) -> None:
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--channel-id", type=int)
    group.add_argument("--slug")
    group.add_argument("--project")
    group.add_argument("--all", dest="all_channels", action="store_true")
    parser.add_argument("--yes", action="store_true", help="confirmar operaciones sobre todos los canales")
