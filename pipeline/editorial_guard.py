"""Opt-in editorial niche guard for newly activated channel content."""
from dataclasses import dataclass

@dataclass(frozen=True)
class EditorialResult:
    allowed: bool
    reason: str = ""
    ambiguous: bool = False

def validate_new_content(script: dict, config, db, slug: str) -> EditorialResult:
    if not getattr(config, "REVIEW_GOVERNANCE_NICHE_GUARD_ENABLED", False):
        return EditorialResult(True)
    # The activation timestamp is mandatory: this guard cannot affect old,
    # awaiting_upload, uploaded_private, scheduled, or existing slot content.
    from api.services.review_governance import activation_at
    channel = db.get_channel_by_slug(slug)
    if not channel or not activation_at(db, channel["id"], slug, config):
        return EditorialResult(True)
    text = " ".join(str(script.get(k) or "") for k in ("titulo_selected", "titulo", "guion")).casefold()
    title = str(script.get("titulo_selected") or script.get("titulo") or "").strip().casefold()
    if title:
        with db._connect() as conn:
            prior = conn.execute(
                """SELECT id FROM videos WHERE channel_id=? AND lower(trim(titulo_final))=?
                   AND status NOT IN ('draft','failed') LIMIT 1""",
                (channel["id"], title),
            ).fetchone()
        if prior:
            from api.services.lifecycle_monitor import emit_alert
            emit_alert(db, entity_type="system", entity_id=channel["id"], channel_id=channel["id"],
                       alert_type="editorial_repeat_review", severity="warning",
                       title=f"Título repetido pendiente de revisión: {slug}",
                       message="El título nuevo coincide exactamente con un vídeo existente; TTS/render bloqueados.",
                       metadata={"existing_video_id": prior["id"], "title": title})
            return EditorialResult(False, "Título repetido: revisión editorial requerida")
    keywords = [str(k).casefold() for k in getattr(config, "REVIEW_GOVERNANCE_NICHE_KEYWORDS", ()) if str(k).strip()]
    if not keywords:
        return EditorialResult(True)
    if any(k in text for k in keywords):
        return EditorialResult(True)
    from api.services.lifecycle_monitor import emit_alert
    ambiguous = bool(getattr(config, "REVIEW_GOVERNANCE_AMBIGUOUS_REVIEW", True))
    emit_alert(db, entity_type="system", entity_id=channel["id"], channel_id=channel["id"],
               alert_type="editorial_niche_review", severity="warning",
               title=f"Tema fuera de nicho pendiente de revisión: {slug}",
               message="El contenido nuevo no contiene señales claras del nicho configurado.",
               metadata={"slug": slug, "keywords": keywords})
    return EditorialResult(False, "Tema fuera de nicho: revisión editorial requerida", ambiguous)
