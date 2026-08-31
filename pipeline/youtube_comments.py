"""YouTube Comment Manager — Auto-comment & reply system.

Posts an engaging first comment after video upload and responds to viewer
comments using LLM-generated replies aligned with channel tone.

Note: The YouTube Data API v3 does NOT support pinning comments.
The first comment is posted but must be pinned manually in YouTube Studio.

Quota costs (per operation):
  - commentThreads().insert() → 50 units
  - commentThreads().list()   → 1 unit
  - comments().insert()        → 50 units
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

from config.llm_client import create_llm_client
from googleapiclient.errors import HttpError

from config.settings import LLM_MODEL, TOKENS_DIR
from pipeline.youtube_playlists import _load_credentials

# ── Quota tracking (passive diagnostic — no behavioral change) ──
from api.services.quota_tracker import track_quota

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════
# LLM prompts
# ═══════════════════════════════════════════════════════════════════

_FIRST_COMMENT_SYSTEM = """Eres el creador de un canal de YouTube. Acabas de publicar un video.
Tu trabajo es escribir UN comentario para publicar en tu propio video que:

1. Haga una pregunta provocadora sobre el tema del video que invite a la audiencia a compartir sus experiencias u opiniones
2. Use un tono auténtico y cercano, como si estuvieras hablando con un amigo
3. Use 1-2 emojis con moderación (no más)
4. Tenga máximo 200 caracteres
5. NO incluya hashtags ni links
6. NO suene robótico ni genérico — debe ser específico al contenido del video
7. Suene curioso, no desesperado

Responde SOLO con el texto del comentario, sin comillas ni prefijos."""

_REPLY_SYSTEM = """Eres el creador de un canal de YouTube. Un espectador ha comentado en tu video.

Responde de forma auténtica, natural y humana, como si estuvieras respondiendo tú personalmente desde el móvil, NO como atención al cliente:

1. Lee el comentario y responde a SU contenido específico (menciona algo concreto de lo que dijo).
2. Agradece de forma natural, sin "gracias por comentar" genérico ni fórmulas de manual.
3. Tono del canal: {channel_tone}
4. Sé elegante y cordial, cercano.
5. Máximo 150 caracteres.
6. Usa signos de cierre SIEMPRE ("!" y "?") pero NUNCA los de apertura ("¡" y "¿"). Ej: "No sabía eso!" o "Que interesante, no?"
7. De vez en cuando (no en todas las respuestas) incluye 1 falta de ortografía leve y creíble (ej: "que bien" en vez de "qué bien", "a ver si" bien escrito, "miralo" en vez de "míralo", "va a ser" → "va a ser", omisión de tildes). Que parezca un despiste de persona real, nunca algo forzado ni en mayúsculas.
8. NO uses hashtags, links, ni emojis en exceso (máx 1).
9. NO suenes robótico, ni vendedor, ni repetitivo.

Responde SOLO con el texto de la respuesta, sin comillas ni prefijos."""


# ═══════════════════════════════════════════════════════════════════
# YouTubeCommentManager
# ═══════════════════════════════════════════════════════════════════

class YouTubeCommentManager:
    """Manage comments for a YouTube channel — post, reply, moderate."""

    def __init__(self, channel_slug: str):
        self.slug = channel_slug
        self._token_path = TOKENS_DIR / f"{channel_slug}.pickle"
        self._service: Any = None
        self._config: Any = None
        self._llm_client: OpenAI | None = None

    # ── Config ────────────────────────────────────────────────────

    @property
    def config(self):
        if self._config is None:
            from config.config_bridge import get_channel_config
            self._config = get_channel_config(self.slug)
        return self._config

    def _get_channel_tone(self) -> str:
        return getattr(self.config, "CANAL_NARRATIVE_STYLE", "informativo y cercano")

    # ── Auth ───────────────────────────────────────────────────────

    def authenticate(self) -> bool:
        from api.services.egress_delegation import fail_closed_if_managed
        fail_closed_if_managed(self.slug, "comments")
        creds = _load_credentials(self._token_path)
        if creds is None:
            return False
        from googleapiclient.discovery import build
        self._service = build("youtube", "v3", credentials=creds, cache_discovery=False)
        return True

    def _ensure_auth(self):
        if self._service is None and not self.authenticate():
            raise RuntimeError(f"Cannot authenticate channel {self.slug}")

    # ── LLM ───────────────────────────────────────────────────────

    def _get_llm(self) -> OpenAI:
        if self._llm_client is None:
            self._llm_client = create_llm_client(
                timeout=30.0,
                max_retries=1,
            )
        return self._llm_client

    def _generate_comment_text(self, system_prompt: str, user_prompt: str,
                                temperature: float = 0.9) -> str:
        """Generate comment text via LLM."""
        try:
            client = self._get_llm()
            response = client.chat.completions.create(
                model=LLM_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=temperature,
                max_tokens=300,
            )
            text = response.choices[0].message.content.strip()
            # Strip quotes if the model wrapped it
            if text.startswith('"') and text.endswith('"'):
                text = text[1:-1]
            return text[:200]
        except Exception as exc:
            logger.warning("[%s] LLM comment generation failed: %s", self.slug, exc)
            return ""

    # ── Comment operations ────────────────────────────────────────

    def post_comment(self, yt_video_id: str, text: str) -> dict:
        """Post a top-level comment on a video.

        Quota: 50 units.

        Returns {yt_comment_id, text}.
        """
        # ── Delegación al agente egress (canal gestionado) ──
        from api.services.egress_delegation import egress_client_for as _ecf
        _egress = _ecf(self.slug)
        if _egress is not None:
            _r = _egress.api_call("post_comment", {"kwargs": {"video_id": yt_video_id, "text": text}})
            if not _r.get("ok"):
                raise RuntimeError(_r.get("error", "post_comment vía agente falló"))
            return {"yt_comment_id": _r.get("result", {}).get("comment_id", ""), "text": text}

        self._ensure_auth()

        body = {
            "snippet": {
                "videoId": yt_video_id,
                "topLevelComment": {
                    "snippet": {
                        "textOriginal": text[:2000],
                    },
                },
            },
        }

        resp = self._service.commentThreads().insert(
            part="snippet",
            body=body,
        ).execute()

        # ── Track quota (diagnostic) ──────────────────────────────
        track_quota(self.slug, "commentThreads.insert", 50,
                    yt_id=yt_video_id, caller="post_comment")

        comment_id = resp["id"]
        logger.info("[%s] Posted comment on %s: %s...", self.slug, yt_video_id, text[:60])
        return {"yt_comment_id": comment_id, "text": text}

    def reply_to_comment(self, parent_comment_id: str, text: str) -> dict:
        """Reply to an existing comment.

        Quota: 50 units.

        Returns {yt_comment_id, text}.
        """
        # ── Delegación al agente egress (canal gestionado) ──
        from api.services.egress_delegation import egress_client_for as _ecf
        _egress = _ecf(self.slug)
        if _egress is not None:
            _r = _egress.api_call("reply_comment", {"kwargs": {"parent_id": parent_comment_id, "text": text}})
            if not _r.get("ok"):
                raise RuntimeError(_r.get("error", "reply_comment vía agente falló"))
            return {"yt_comment_id": _r.get("result", {}).get("comment_id", ""), "text": text}

        self._ensure_auth()

        body = {
            "snippet": {
                "parentId": parent_comment_id,
                "textOriginal": text[:2000],
            },
        }

        resp = self._service.comments().insert(
            part="snippet",
            body=body,
        ).execute()

        # ── Track quota (diagnostic) ──────────────────────────────
        track_quota(self.slug, "comments.insert", 50,
                    yt_id=parent_comment_id, caller="reply_to_comment")

        comment_id = resp["id"]
        return {"yt_comment_id": comment_id, "text": text}

    def list_comments(self, yt_video_id: str, max_results: int = 100) -> list[dict]:
        """List comments on a video.

        Quota: 1 unit per 100 results.

        Returns [{yt_comment_id, author, text, like_count, reply_count, has_channel_reply}, ...].
        """
        self._ensure_auth()

        try:
            resp = self._service.commentThreads().list(
                part="snippet,replies",
                videoId=yt_video_id,
                maxResults=min(max_results, 100),
                order="time",
            ).execute()

            # ── Track quota (diagnostic) ──────────────────────────
            track_quota(self.slug, "commentThreads.list", 1,
                        yt_id=yt_video_id, caller="list_comments")
        except HttpError as exc:
            if exc.resp.status == 403:
                # Comments may be disabled
                logger.warning("[%s] Comments disabled for video %s", self.slug, yt_video_id)
                return []
            raise

        comments = []
        for item in resp.get("items", []):
            top = item["snippet"]["topLevelComment"]
            snippet = top["snippet"]
            has_replies = "replies" in item

            comments.append({
                "yt_comment_id": item["id"],
                "author": snippet.get("authorDisplayName", ""),
                "text": snippet.get("textDisplay", ""),
                "like_count": snippet.get("likeCount", 0),
                "reply_count": item["snippet"].get("totalReplyCount", 0),
                "has_channel_reply": has_replies,
                "replies": [
                    {
                        "yt_comment_id": r["id"],
                        "author": r["snippet"].get("authorDisplayName", ""),
                        "text": r["snippet"].get("textDisplay", ""),
                    }
                    for r in item.get("replies", {}).get("comments", [])
                ] if has_replies else [],
            })

        return comments

    # ── High-level operations ─────────────────────────────────────

    def post_first_comment(self, yt_video_id: str,
                            script_text: str = None,
                            db_video_id: int = None) -> dict:
        """Post an engaging first comment on a newly uploaded video.

        Generates comment text via LLM using the video's script content.
        Idempotent — checks comment_log in DB before posting.

        Returns {yt_comment_id, text, pinned_required} or {skipped: True, reason: ...}.
        """
        # Idempotency check
        if db_video_id is not None:
            from database.db_extended import ExtendedDatabase
            db = ExtendedDatabase()
            if db.has_first_comment(db_video_id):
                logger.info("[%s] First comment already exists for video %d", self.slug, db_video_id)
                return {"skipped": True, "reason": "already_posted"}

        # Generate comment text
        if script_text:
            user_prompt = f"""CONTENIDO DEL VIDEO:
{script_text[:2000]}

Escribe el comentario."""
        else:
            user_prompt = "Escribe un comentario intrigante para un video nuevo de YouTube."

        comment_text = self._generate_comment_text(_FIRST_COMMENT_SYSTEM, user_prompt)
        if not comment_text:
            comment_text = "¿Qué opinas de esta historia? ¿Te ha pasado algo parecido? Cuéntalo en los comentarios 👇"

        try:
            result = self.post_comment(yt_video_id, comment_text)
            logger.info("[%s] First comment posted on %s", self.slug, yt_video_id)
            return {
                "yt_comment_id": result["yt_comment_id"],
                "text": comment_text,
                "pinned_required": True,  # API doesn't support pinning
            }
        except HttpError as exc:
            if exc.resp.status == 403 and "commentsDisabled" in str(exc):
                logger.warning("[%s] Comments disabled for video %s", self.slug, yt_video_id)
                return {"skipped": True, "reason": "comments_disabled"}
            raise

    def reply_to_comments(self, yt_video_id: str,
                           max_replies: int = 5,
                           db_video_id: int = None) -> dict:
        """Reply to viewer comments that haven't received a response yet.

        Uses LLM to generate personalized replies in the channel's tone.
        Filters out short/spam comments.

        Returns {replied_to: N, skipped: N, errors: [...]}.
        """
        if max_replies <= 0:
            return {"replied_to": 0, "skipped": 0, "errors": []}

        comments = self.list_comments(yt_video_id, max_results=100)
        if not comments:
            return {"replied_to": 0, "skipped": 0, "errors": []}

        channel_tone = self._get_channel_tone()
        replied, skipped, errors = 0, 0, []

        for c in comments:
            if replied >= max_replies:
                break

            # Skip if already has replies from anyone
            if c["reply_count"] > 0:
                skipped += 1
                continue

            text = c.get("text", "").strip()

            # Filter out spam / low-quality comments
            if len(text) < 10:
                skipped += 1
                continue
            if "http://" in text or "https://" in text:
                skipped += 1
                continue
            # Heurística: si tiene muchos caracteres no latinos, skip
            non_latin = sum(1 for ch in text if ord(ch) > 127)
            if non_latin > len(text) * 0.6 and len(text) < 20:
                skipped += 1
                continue

            try:
                system_prompt = _REPLY_SYSTEM.format(channel_tone=channel_tone)
                user_prompt = f"COMENTARIO DEL ESPECTADOR: {text}\n\nEscribe tu respuesta:"
                reply_text = self._generate_comment_text(system_prompt, user_prompt, temperature=0.85)

                if not reply_text:
                    reply_text = f"¡Gracias por ver el video! 😊"

                self.reply_to_comment(c["yt_comment_id"], reply_text)
                replied += 1
                logger.debug("[%s] Replied to: %s...", self.slug, text[:50])

            except HttpError as exc:
                if exc.resp.status == 403:
                    errors.append(f"Permission denied replying to {c['yt_comment_id']}")
                else:
                    errors.append(str(exc))
            except Exception as exc:
                errors.append(str(exc))

        logger.info("[%s] Comment replies: replied=%d skipped=%d errors=%d",
                     self.slug, replied, skipped, len(errors))
        return {"replied_to": replied, "skipped": skipped, "errors": errors}
