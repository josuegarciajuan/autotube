"""Feedback de mejoras (F9) — alertas de sistema + avisos de estado.

Cada mejora del plan (F0–F9) tiene un identificador estable y publica su estado
por las DOS vías que permite el sistema:

* **Alerta de sistema** (``pipeline_alerts`` vía ``emit_alert``): visible en
  Monitor, reconocible/resoluble, deduplicada por mejora.
* **Aviso de estado** (``system_state['improvement_feedback']``): lo consume el
  centro de estado del panel; NO es un strike ni enforcement.

Resolver la alerta NO borra el aviso de estado ni marca la mejora como validada:
son planos distintos.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("autotube.improvement_feedback")

STATE_KEY = "improvement_feedback"
DEPLOYED_AT_KEY = "improvement_feedback_deployed_at"

# Estados posibles (los que pinta el panel).
STATUS_PENDING = "pendiente"
STATUS_DEPLOYED = "desplegada"
STATUS_VERIFIED = "verificada_tecnicamente"
STATUS_AWAITING_GENERATION = "esperando_generacion"
STATUS_AWAITING_DATA = "esperando_datos"
STATUS_NEEDS_REVIEW = "requiere_revision"
STATUS_VALIDATED = "validada"
STATUS_INCONCLUSIVE = "no_concluyente"

# Catálogo estable del plan. `affects_content` = su efecto se mide con vídeos
# nuevos (mientras la fábrica esté parada, queda en espera_generacion).
IMPROVEMENTS: list[dict] = [
    {"id": "F0_pausa_protegida", "phase": "F0", "affects_content": False,
     "title": "Pausa de generación protegida a través de reinicios",
     "detail": "Hold de primera clase; recuperación/reconexión no lo tumban."},
    {"id": "F1_medicion_embudo", "phase": "F1", "affects_content": False,
     "title": "Medición fiable del embudo (fechas, agregación, tráfico)",
     "detail": "Fechas ISO, segmentos sumados y fuentes de tráfico persistidas."},
    {"id": "F2_baseline_monetizacion", "phase": "F2", "affects_content": False,
     "title": "Baseline reconciliado y horas Analytics vs YPP",
     "detail": "Baseline sin pisar; horas YPP solo si están confirmadas en Studio."},
    {"id": "F3_demanda_todas_rutas", "phase": "F3", "affects_content": True,
     "title": "Demanda y trazabilidad en todas las rutas",
     "detail": "Seeding + procedencia en original/viral/maratón."},
    {"id": "F4_coherencia_series", "phase": "F4", "affects_content": True,
     "title": "Gate de nicho estricto y series de contenido",
     "detail": "Difiere antes de publicar fuera de nicho; series por canal."},
    {"id": "F5_packaging_completo", "phase": "F5", "affects_content": True,
     "title": "Completitud de título y overlays",
     "detail": "Sin títulos cortados a medias ni overlays fuera de presupuesto."},
    {"id": "F6_ab_flujo_f1f2", "phase": "F6", "affects_content": True,
     "title": "A/B operativo en el flujo F1/F2",
     "detail": "Variantes persistidas en generación; CTR de ventana post-cambio."},
    {"id": "F7_retencion_temprana", "phase": "F7", "affects_content": True,
     "title": "Retención por caída real y gancho 30/60/90 s",
     "detail": "Ataca la pérdida temprana, no solo las fases finales."},
    {"id": "F8_shorts_conversion", "phase": "F8", "affects_content": True,
     "title": "Shorts: vínculo relevante, CTA y conversión",
     "detail": "Enlace público relevante; conversión 'desconocida' si no hay dato."},
    {"id": "F9_feedback_doble", "phase": "F9", "affects_content": False,
     "title": "Feedback doble, evaluación agendada y despliegue",
     "detail": "Alertas + avisos por mejora y revisiones 48h/7d/14d/28d."},
]

REVIEW_CHECKPOINTS = (("48h", 2), ("7d", 7), ("14d", 14), ("28d", 28))

_REVIEW_BRIEF = (
    "Revisión {kind}: integridad y publicación; señales iniciales de impresiones, "
    "CTR y retención. Recuerda que el reloj de rendimiento cuenta desde la "
    "publicación real de vídeos nuevos, no desde el despliegue. La generación "
    "sigue parada salvo que el operador la reanude."
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def deploy_status() -> str:
    return STATUS_AWAITING_GENERATION


def publish_all(db, start_iso: str | None = None) -> dict:
    """Publica el estado de todas las mejoras por las dos vías."""
    start = start_iso or _now_iso()
    items = []
    for idx, imp in enumerate(IMPROVEMENTS):
        status = STATUS_AWAITING_GENERATION if imp["affects_content"] else STATUS_VERIFIED
        item = {
            "id": imp["id"], "phase": imp["phase"], "title": imp["title"],
            "detail": imp["detail"], "status": status,
            "affects_content": imp["affects_content"], "published_at": start,
        }
        items.append(item)
        _emit_improvement_alert(db, idx, item)

    payload = {"updated_at": _now_iso(), "deployed_at": start, "items": items}
    db.set_system_state(STATE_KEY, json.dumps(payload, ensure_ascii=False))
    db.set_system_state(DEPLOYED_AT_KEY, start)
    schedule_evaluation_checkpoints(db, start)
    logger.info("Feedback de mejoras publicado (%d)", len(items))
    return payload


def get_status(db) -> dict:
    raw = db.get_system_state(STATE_KEY)
    try:
        return json.loads(raw) if raw else {"updated_at": None, "items": []}
    except (TypeError, ValueError):
        return {"updated_at": None, "items": []}


def set_improvement_status(db, improvement_id: str, status: str, note: str = "") -> dict:
    """Actualiza el estado de una mejora (operador o revisión)."""
    payload = get_status(db)
    for item in payload.get("items", []):
        if item.get("id") == improvement_id:
            item["status"] = status
            if note:
                item["note"] = note[:400]
            item["updated_at"] = _now_iso()
    payload["updated_at"] = _now_iso()
    db.set_system_state(STATE_KEY, json.dumps(payload, ensure_ascii=False))
    return payload


def _emit_improvement_alert(db, idx: int, item: dict) -> None:
    try:
        from api.services.lifecycle_monitor import emit_alert
        emit_alert(
            db=db, entity_type="system", entity_id=9000 + idx,
            alert_type="improvement_deployed", severity="info",
            title=f"[{item['phase']}] {item['title']}",
            message=f"Estado: {item['status']}. {item['detail']}",
            metadata={"improvement_id": item["id"], "status": item["status"]},
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("improvement alert failed (%s): %s", item.get("id"), exc)


def schedule_evaluation_checkpoints(db, start_iso: str) -> list[dict]:
    """Programa las revisiones 48h/7d/14d/28d como recordatorios (idempotente)."""
    try:
        base = datetime.fromisoformat(str(start_iso)[:19])
        if base.tzinfo is None:
            base = base.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        base = datetime.now(timezone.utc)

    created: list[dict] = []
    with db._connect() as conn:
        for i, (kind, days) in enumerate(REVIEW_CHECKPOINTS):
            due = (base + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
            title = f"Revisión de mejoras — {kind}"
            meta = json.dumps({
                "severity": "warning",
                "improvement_review": kind,
                "review": "specs/experimento-recuperacion-alcance.md",
                "brief": _REVIEW_BRIEF.format(kind=kind),
            }, ensure_ascii=False)
            cur = conn.execute(
                """INSERT OR IGNORE INTO scheduled_reminders
                     (entity_type, entity_id, title, message, alert_type,
                      due_at, status, metadata_json)
                   VALUES ('system', ?, ?, ?, 'improvement_review', ?, 'pending', ?)""",
                (9500 + i, title, _REVIEW_BRIEF.format(kind=kind), due, meta),
            )
            created.append({"kind": kind, "due_at": due, "inserted": bool(cur.rowcount)})
        conn.commit()
    return created


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    parser = argparse.ArgumentParser(description="Publica el feedback doble de mejoras")
    parser.add_argument("--deploy", action="store_true", help="marca el despliegue y publica")
    parser.add_argument("--status", action="store_true", help="muestra el estado")
    args = parser.parse_args(argv)

    from database.db_extended import ExtendedDatabase
    db = ExtendedDatabase()
    if args.deploy:
        res = publish_all(db)
        print(f"Feedback publicado: {len(res['items'])} mejoras")
    res = get_status(db)
    for it in res.get("items", []):
        print(f"  [{it['phase']}] {it['status']:<22} {it['title']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
