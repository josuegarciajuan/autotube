#!/usr/bin/env python3
"""Crea los 4 canales nuevos 2026 en LOCAL, sin egress y sin YouTube.

- Crea/actualiza las filas de `channels` (active=0, egress_managed=0).
- Sincroniza el config Python -> `config_json`.
- Genera banner/avatar/descripción con `channel_profile_generator`.
- Desvincula (con backup) las entradas de egress de esos slugs para que sean
  locales hasta que se enganchen al nodo encuestaCitas.

Uso (desde el árbol principal):
    python3 scripts/create_2026_channels.py
    python3 scripts/create_2026_channels.py --keep-egress  # no tocar el registro
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CHANNELS = [
    {"slug": "canal6", "name": "Viral o Falso"},
    {"slug": "canal7", "name": "¿Sabías Esto?"},
    {"slug": "canal8", "name": "Internet Oculto"},
    {"slug": "canal9", "name": "Un Minuto de Locura"},
]
REGISTRY = ROOT / "config" / "egress_agents.json"
BACKUP_DIR = Path("/root/.config/autotube")


def _detach_registry(slugs: list[str]) -> str:
    """Quita los slugs del registro egress y guarda un backup con sus tokens."""
    if not REGISTRY.exists():
        return ""
    try:
        data = json.loads(REGISTRY.read_text(encoding="utf-8")) or {}
    except Exception:
        return ""
    removed = {s: data.pop(s) for s in slugs if s in data}
    if not removed:
        return ""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = BACKUP_DIR / f"egress-registry-backup-{ts}.json"
    backup.write_text(json.dumps(removed, indent=2, ensure_ascii=False), encoding="utf-8")
    os.chmod(backup, 0o600)
    tmp = REGISTRY.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, REGISTRY)
    return str(backup)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--keep-egress", action="store_true",
                    help="no quitar los slugs de config/egress_agents.json")
    args = ap.parse_args()

    from database.db_extended import ExtendedDatabase
    from config.config_bridge import (
        _config_cache, get_channel_config, sync_config_to_db,
    )
    from pipeline.channel_profile_generator import generate_channel_profile

    db = ExtendedDatabase()
    summary = []

    for spec in CHANNELS:
        slug = spec["slug"]
        name = spec["name"]
        ch = db.get_channel_by_slug(slug)
        if not ch:
            ch_id = db.create_channel(name, slug, {})
            if ch_id is None:
                print(f"❌ no se pudo crear {slug}", file=sys.stderr)
                return 1
            print(f"✅ creado {slug} (id={ch_id})")
        else:
            ch_id = int(ch["id"])
            print(f"↻ existe {slug} (id={ch_id}) — actualizando")

        db.update_channel(ch_id, name=name, slug=slug, active=False)
        synced = sync_config_to_db(slug)
        if synced is None:
            print(f"❌ sync_config_to_db falló para {slug}", file=sys.stderr)
            return 1
        _config_cache.pop(slug, None)

        # Local-only: flag durable a 0 y sin IP/etiqueta de egress.
        db.set_channel_egress_managed(ch_id, False, status="unmanaged")
        try:
            with db._connect() as conn:
                conn.execute(
                    "UPDATE channels SET egress_managed=0, egress_expected_ip=NULL, "
                    "egress_label=NULL, egress_status='unmanaged', active=0, "
                    "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (ch_id,),
                )
                conn.commit()
        except Exception as exc:  # noqa: BLE001
            print(f"⚠️ no se pudo limpiar egress de {slug}: {exc}", file=sys.stderr)

        profile = generate_channel_profile(slug)
        db.update_channel_profile_fields(
            ch_id,
            description=profile.get("description") or "",
            banner_url=profile.get("banner_url") or "",
            avatar_url=profile.get("avatar_url") or "",
        )
        cfg = get_channel_config(slug, force_reload=True)
        desc = str(getattr(cfg, "CHANNEL_ABOUT_SECTION", "") or "")
        kws = list(getattr(cfg, "CHANNEL_KEYWORDS", []) or [])
        kw_len = len(", ".join(str(k) for k in kws)) if kws else 0

        summary.append({
            "slug": slug,
            "id": ch_id,
            "name": name,
            "active": False,
            "egress_managed": False,
            "description_chars": len(desc),
            "description_ok": len(desc) <= 1000,
            "keywords_chars": kw_len,
            "keywords_ok": kw_len <= 500,
            "banner_url": profile.get("banner_url", ""),
            "avatar_url": profile.get("avatar_url", ""),
            "playlists": len(getattr(cfg, "PLAYLISTS", []) or []),
            "pillars": len(getattr(cfg, "CONTENT_PILLARS", []) or []),
        })
        print(
            f"   desc={len(desc)}/1000  keywords={kw_len}/500  "
            f"banner={profile.get('banner_url')}  avatar={profile.get('avatar_url')}"
        )

    backup = "" if args.keep_egress else _detach_registry([c["slug"] for c in CHANNELS])
    if backup:
        print(f"\n🔌 Egress desvinculado (local-only). Backup de tokens: {backup}")
        print("   Para enganchar después: restaurar esas entradas y poner egress_managed=1.")

    report_dir = ROOT / "reports"
    report_dir.mkdir(exist_ok=True)
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "channels": summary,
        "egress_registry_backup": backup,
    }
    (report_dir / "canales-2026-setup.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"\n📄 Resumen: {report_dir / 'canales-2026-setup.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
