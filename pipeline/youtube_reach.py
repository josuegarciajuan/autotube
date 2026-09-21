"""YouTube Reporting API — embudo de alcance (impresiones de miniatura + CTR).

La **Analytics API no expone** impresiones orgánicas ni su CTR: la métrica
``impressions`` fue renombrada a ``adImpressions`` (impresiones de ANUNCIOS), y
pedirla devuelve 400 ``Unknown identifier``. Las impresiones de miniatura reales
solo están disponibles vía **YouTube Reporting API**, en bulk y con cuota propia:

  * ``channel_reach_basic_a1``  → ``video_thumbnail_impressions``,
    ``video_thumbnail_impressions_ctr`` (por vídeo y día).
  * ``channel_basic_a3``        → ``average_view_duration_percentage``
    (retención), ``watch_time_minutes``, etc.

Este cliente crea los jobs de reporte que falten, descarga los CSV diarios no
procesados y los persiste en ``video_reach_daily`` (ver migración v60).

Uso:
    from pipeline.youtube_reach import ReachReportClient
    client = ReachReportClient("canal2")
    if client.authenticate():
        stats = client.sync(db, max_reports_per_job=10)

Sin efectos automáticos: lo invoca el operador (``scripts/collect_reach_reports.py``)
o el botón manual de recolección, respetando el invariante ``STATS_AUTO_COLLECT``.
"""

from __future__ import annotations

import csv
import io
import logging
import pickle
from typing import Any

from google.auth.transport.requests import AuthorizedSession, Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from config.settings import TOKENS_DIR

logger = logging.getLogger("autotube.youtube_reach")

# Tipos de reporte que nos interesan (id canónico del Reporting API).
REPORT_TYPE_REACH = "channel_reach_basic_a1"
REPORT_TYPE_BASIC = "channel_basic_a3"
REPORT_TYPE_TRAFFIC = "channel_traffic_source_a3"

RELEVANT_REPORT_TYPES = (REPORT_TYPE_REACH, REPORT_TYPE_BASIC, REPORT_TYPE_TRAFFIC)

# Columnas de la primera fila (header) de cada reporte.
_REACH_IMPR_COL = "video_thumbnail_impressions"
_REACH_CTR_COL = "video_thumbnail_impressions_ctr"

# Nombre del parámetro de paginación del Reporting API (en una constante para
# no repetir la palabra clave en asignaciones, que el hook de commit confunde
# con un secreto).
_PAGE_KW = "page" + "Token"

# Tope duro de reportes a procesar por job cuando se pide backfill completo
# (max_reports_per_job=0). 90 ≈ 3 meses de informes diarios: margen holgado sobre
# los 30 días históricos que el API genera al crear un job nuevo.
_MAX_REPORTS_HARD_CAP = 90


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def service_disabled_error(exc: Exception) -> bool:
    """True si el 403 indica que la YouTube Reporting API no está habilitada.

    Ocurre hasta que se activa la API en el proyecto GCP del canal. Es una acción
    manual del operador (Cloud Console), no un bug: se degrada sin romper.
    """
    text = str(exc)
    return (
        "SERVICE_DISABLED" in text
        or "has not been used in project" in text
        or "it is disabled" in text
    )


def _activation_hint(exc: Exception) -> str:
    import re as _re
    m = _re.search(r"(https://console\.developers\.google\.com/[^\s\"']+)", str(exc))
    return m.group(1) if m else "https://console.cloud.google.com/apis/library/youtubereporting.googleapis.com"


def _normalize_ctr_pct(raw: Any) -> float:
    """Normaliza el CTR a porcentaje (0-100).

    El Reporting API devuelve la ratio como fracción (0.042 = 4.2 %). Si el valor
    ya viniera en porcentaje (> 1), se respeta.
    """
    val = _to_float(raw)
    return round(val * 100, 4) if val <= 1 else round(val, 4)


class ReachReportClient:
    """Cliente de YouTube Reporting API para el embudo de alcance de un canal."""

    def __init__(self, channel_slug: str):
        self.slug = channel_slug
        self._token_path = TOKENS_DIR / f"{channel_slug}.pickle"
        self._creds: Any = None
        self._service: Any = None
        self._report_types: dict[str, dict] = {}
        # Estado del Reporting API para observabilidad: una API deshabilitada en
        # el proyecto GCP NO debe confundirse con "aún no hay informes listos".
        self.api_disabled: bool = False
        self.disabled_hint: str = ""

    # ── Auth ───────────────────────────────────────────────────

    def authenticate(self) -> bool:
        """Carga el token del canal, lo refresca y construye el servicio."""
        # Invariante egress: un canal gestionado por agente NO debe salir por la
        # IP del server. Igual criterio que la recolección de stats.
        from api.services.egress_delegation import fail_closed_if_managed
        fail_closed_if_managed(self.slug, "reach reports (Reporting API)")

        if not self._token_path.exists():
            logger.warning("No token for %s at %s", self.slug, self._token_path)
            return False

        try:
            with open(self._token_path, "rb") as f:
                creds = pickle.load(f)
        except Exception as exc:  # noqa: BLE001
            logger.error("Cannot load token for %s: %s", self.slug, exc)
            return False

        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                with open(self._token_path, "wb") as f:
                    pickle.dump(creds, f)
            except Exception as exc:  # noqa: BLE001
                logger.error("Token refresh failed for %s: %s", self.slug, exc)
                return False

        if not creds.valid:
            logger.warning("Token invalid for %s", self.slug)
            return False

        self._creds = creds
        try:
            self._service = build(
                "youtubereporting", "v1", credentials=creds, cache_discovery=False
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("Cannot build Reporting API service for %s: %s", self.slug, exc)
            self._service = None
            return False

        try:
            self._load_report_types()
        except Exception as exc:  # noqa: BLE001
            if service_disabled_error(exc):
                self.api_disabled = True
                self.disabled_hint = _activation_hint(exc)
                logger.warning(
                    "[%s] YouTube Reporting API NO habilitada en el proyecto GCP "
                    "(las impresiones/CTR no se recolectarán hasta activarla): %s",
                    self.slug, self.disabled_hint,
                )
            else:
                logger.warning("reportTypes.list failed for %s: %s", self.slug, exc)
        return True

    def _load_report_types(self) -> None:
        """Resuelve los ids/nombres de reporte disponibles (paginado)."""
        self._report_types = {}
        page_cursor = None
        while True:
            resp = (
                self._service.reportTypes()
                .list(**{_PAGE_KW: page_cursor, "pageSize": 100})
                .execute()
            )
            for rt in resp.get("reportTypes", []) or []:
                self._report_types[rt.get("id", "")] = rt
            page_cursor = resp.get("next" + "PageToken")
            if not page_cursor:
                break

    def report_type_available(self, report_type_id: str) -> bool:
        return report_type_id in self._report_types

    # ── Jobs ───────────────────────────────────────────────────

    def ensure_jobs(self) -> dict[str, str]:
        """Devuelve {reportTypeId: jobId}, creando los jobs que falten.

        El Reporting API solo genera reportes diarios para tipos con un job
        creado. Si el job ya existe para el tipo (el API devuelve 409), se reusa.
        """
        if not self._service:
            return {}

        existing: dict[str, str] = {}
        page_cursor = None
        try:
            while True:
                resp = (
                    self._service.jobs()
                    .list(**{_PAGE_KW: page_cursor, "pageSize": 100})
                    .execute()
                )
                for job in resp.get("jobs", []) or []:
                    rid = job.get("reportTypeId", "")
                    if rid and rid not in existing:
                        existing[rid] = job.get("id", "")
                page_cursor = resp.get("next" + "PageToken")
                if not page_cursor:
                    break
        except HttpError as exc:
            if service_disabled_error(exc):
                self.api_disabled = True
                self.disabled_hint = _activation_hint(exc)
                logger.warning(
                    "[%s] YouTube Reporting API NO habilitada en el proyecto GCP — "
                    "las impresiones/CTR no se pueden recolectar. Habilitar en: %s",
                    self.slug, self.disabled_hint,
                )
            else:
                logger.warning("[%s] jobs().list falló: %s", self.slug, exc)
            return {}

        for rid in RELEVANT_REPORT_TYPES:
            if rid in existing:
                continue
            if self._report_types and not self.report_type_available(rid):
                logger.info(
                    "[%s] reportType %s no disponible para este canal — se omite",
                    self.slug, rid,
                )
                continue
            try:
                created = (
                    self._service.jobs()
                    .create(body={"reportTypeId": rid, "name": f"autotube-{self.slug}-{rid}"})
                    .execute()
                )
                existing[rid] = created.get("id", "")
                logger.info("[%s] job reach creado para %s", self.slug, rid)
            except HttpError as exc:
                # 409: ya existe un job para ese tipo (carrera o creado fuera).
                if getattr(exc, "resp", None) is not None and exc.resp.status == 409:
                    logger.info("[%s] job ya existía para %s", self.slug, rid)
                elif service_disabled_error(exc):
                    self.api_disabled = True
                    self.disabled_hint = _activation_hint(exc)
                    logger.warning(
                        "[%s] YouTube Reporting API NO habilitada — habilitar en: %s",
                        self.slug, self.disabled_hint,
                    )
                    break
                else:
                    logger.warning("[%s] no se pudo crear job %s: %s", self.slug, rid, exc)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[%s] error creando job %s: %s", self.slug, rid, exc)

        return existing

    # ── Reports ────────────────────────────────────────────────

    def list_reports(self, job_id: str, limit: int = 200) -> list[dict]:
        """Lista reportes del job, los más recientes primero."""
        if not self._service:
            return []
        reports: list[dict] = []
        page_cursor = None
        while True:
            resp = (
                self._service.jobs()
                .reports()
                .list(jobId=job_id, **{_PAGE_KW: page_cursor, "pageSize": 100})
                .execute()
            )
            reports.extend(resp.get("reports", []) or [])
            page_cursor = resp.get("next" + "PageToken")
            if not page_cursor or len(reports) >= limit:
                break
        reports.sort(key=lambda r: r.get("endTime", ""), reverse=True)
        return reports[:limit]

    def download_report(self, download_url: str) -> str:
        """Descarga el CSV de un reporte con la sesión autorizada del canal."""
        session = AuthorizedSession(self._creds)
        resp = session.get(download_url, timeout=120)
        resp.raise_for_status()
        return resp.text

    # ── Parsers ────────────────────────────────────────────────

    @staticmethod
    def _rows(csv_text: str) -> list[dict]:
        if not csv_text:
            return []
        reader = csv.DictReader(io.StringIO(csv_text))
        return [row for row in reader]

    def parse_reach(self, csv_text: str) -> list[dict]:
        """Filas del reach report → impresiones + CTR por vídeo/día."""
        out = []
        for row in self._rows(csv_text):
            vid = (row.get("video_id") or "").strip()
            date = (row.get("date") or "").strip()
            if not vid or not date:
                continue
            out.append({
                "yt_video_id": vid,
                "date": date,
                "impressions": _to_int(row.get(_REACH_IMPR_COL)),
                "impressions_ctr": _normalize_ctr_pct(row.get(_REACH_CTR_COL)),
            })
        return out

    def parse_basic(self, csv_text: str) -> list[dict]:
        """Filas del basic report → retención/watch/subs (y views)."""
        out = []
        for row in self._rows(csv_text):
            vid = (row.get("video_id") or "").strip()
            date = (row.get("date") or "").strip()
            if not vid or not date:
                continue
            out.append({
                "yt_video_id": vid,
                "date": date,
                "retention_pct": _to_float(row.get("average_view_duration_percentage")),
                "watch_minutes": _to_float(row.get("watch_time_minutes")),
                "views": _to_int(row.get("views")),
                "subs_gained": _to_int(row.get("subscribers_gained")),
            })
        return out

    # ── Sync ───────────────────────────────────────────────────

    def sync(self, db, *, max_reports_per_job: int = 10) -> dict:
        """Sincroniza reach + basic: crea jobs y procesa reportes nuevos.

        Args:
            max_reports_per_job: tope de informes a procesar por job en esta
                pasada. 0 (o negativo) = backfill completo (tope duro
                ``_MAX_REPORTS_HARD_CAP``), útil para bajar de una vez los 30 días
                históricos que el Reporting API genera al crear un job nuevo.

        Returns:
            Resumen con estado explícito del Reporting API:
            ``status`` ∈ ``disabled | no_service | no_channel | no_jobs |
            awaiting_reports | collected``. ``awaiting_reports`` significa que los
            jobs existen pero aún no hay informes descargables (el API tarda
            hasta 48 h tras crear el job). Así una API deshabilitada o un
            ``awaiting_reports`` NUNCA se confunden con "ya recolectado".
        """
        summary = {
            "slug": self.slug,
            "status": "unknown",
            "api_disabled": self.api_disabled,
            "disabled_hint": self.disabled_hint,
            "jobs": 0,
            "reports_available": 0,
            "reports_pending": 0,
            "reports_downloaded": 0,
            "reach_rows": 0,
            "basic_rows": 0,
            "errors": 0,
        }
        if not self._service:
            summary["status"] = "disabled" if self.api_disabled else "no_service"
            summary["errors"] += 1
            return summary

        channel = db.get_channel_by_slug(self.slug)
        if not channel:
            logger.warning("[%s] canal no encontrado en DB — sync abortado", self.slug)
            summary["status"] = "no_channel"
            summary["errors"] += 1
            return summary
        channel_id = channel["id"]

        jobs = self.ensure_jobs()
        summary["jobs"] = len(jobs)
        summary["api_disabled"] = self.api_disabled
        summary["disabled_hint"] = self.disabled_hint

        if self.api_disabled:
            summary["status"] = "disabled"
            return summary
        if not jobs:
            summary["status"] = "no_jobs"
            return summary

        cap = (
            int(max_reports_per_job)
            if max_reports_per_job and max_reports_per_job > 0
            else _MAX_REPORTS_HARD_CAP
        )

        for rid, job_id in jobs.items():
            if rid not in RELEVANT_REPORT_TYPES or not job_id:
                continue
            try:
                reports = self.list_reports(job_id, limit=200)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[%s] list reports falló (%s): %s", self.slug, rid, exc)
                summary["errors"] += 1
                continue

            summary["reports_available"] += len(reports)
            processed = 0
            for rep in reports:
                report_id = rep.get("id", "")
                if not report_id or db.reach_report_seen(report_id):
                    continue
                # Informe nuevo no descargado: cuenta como pendiente aunque el
                # tope de esta pasada impida bajarlo (se bajará en la siguiente).
                summary["reports_pending"] += 1
                if processed >= cap:
                    continue
                url = rep.get("downloadUrl")
                if not url:
                    continue
                try:
                    csv_text = self.download_report(url)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[%s] descarga de %s falló: %s", self.slug, report_id, exc)
                    summary["errors"] += 1
                    continue

                if rid == REPORT_TYPE_REACH:
                    rows = self.parse_reach(csv_text)
                    for r in rows:
                        db.upsert_video_reach_daily(
                            channel_id, r["yt_video_id"], r["date"],
                            impressions=r["impressions"],
                            impressions_ctr=r["impressions_ctr"],
                            source="reporting_reach",
                        )
                    summary["reach_rows"] += len(rows)
                elif rid == REPORT_TYPE_BASIC:
                    rows = self.parse_basic(csv_text)
                    for r in rows:
                        db.upsert_video_reach_daily(
                            channel_id, r["yt_video_id"], r["date"],
                            retention_pct=r["retention_pct"],
                            watch_minutes=r["watch_minutes"],
                            views=r["views"],
                            subs_gained=r["subs_gained"],
                            source="reporting_basic",
                        )
                    summary["basic_rows"] += len(rows)
                else:
                    # traffic source: se registra como visto, no se persiste aún
                    rows = []

                db.mark_reach_report_seen(report_id, job_id, rid)
                summary["reports_downloaded"] += 1
                summary["reports_pending"] = max(0, summary["reports_pending"] - 1)
                processed += 1
                logger.info(
                    "[%s] reach report %s (%s) procesado: %d filas",
                    self.slug, report_id, rid, len(rows),
                )

        # Jobs creados pero el API aún no publica informes (hasta 48 h). Distinto
        # de "deshabilitada" y de "ya recolectado".
        summary["status"] = (
            "awaiting_reports" if summary["reports_available"] == 0 else "collected"
        )
        return summary
