"""Project-scoped admission control for billable YouTube video uploads.

The dispatcher owns quota reservation lifecycle; its transport callback owns the
network boundary and *must* call ``request_started`` immediately before its
first billable request.  An exception before that signal releases quota.  Every
outcome after it is conservatively consumed because YouTube may have accepted
the request even when the client cannot determine the result.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


UPLOAD_OPERATION = "videos.insert"
UPLOAD_UNITS = 1600


class UploadDispatchBlocked(RuntimeError):
    """The upload was not admitted and no request may be sent."""


class YouTubeUploadDispatcher:
    """Reserve one project quota slot and invoke a single upload transport."""

    def __init__(
        self,
        channel_slug: str,
        *,
        db=None,
        project_resolver: Callable[[str], str] | None = None,
        quota_day: Callable[[], str] | None = None,
        automatic_budget: int | None = None,
        remediation_mode: bool | None = None,
    ) -> None:
        if db is None:
            from database.db_extended import ExtendedDatabase

            db = ExtendedDatabase()
        if project_resolver is None:
            from api.services.quota_tracker import get_channel_project

            project_resolver = get_channel_project
        if quota_day is None:
            from api.services.quota_tracker import quota_day_pacific

            quota_day = quota_day_pacific
        if automatic_budget is None or remediation_mode is None:
            from config.settings import YT_REMEDIATION_MODE

            # Presupuesto por proyecto (Fase cuota ago 2026): 0 = derivar de
            # YT_PROJECT_BUDGET_UNITS[project] - YT_PROJECT_RESERVED_UNITS.
            automatic_budget = 0 if automatic_budget is None else automatic_budget
            remediation_mode = YT_REMEDIATION_MODE if remediation_mode is None else remediation_mode

        self.channel_slug = channel_slug
        self.db = db
        self.project_resolver = project_resolver
        self.quota_day = quota_day
        self.automatic_budget = automatic_budget
        self.remediation_mode = remediation_mode

    def dispatch(
        self,
        reference_id: str,
        content_class: str,
        transport: Callable[[Callable[[], None]], Any],
    ) -> Any:
        """Admit and execute an upload through the explicit transport boundary."""
        if self.remediation_mode:
            raise UploadDispatchBlocked(
                "YouTube remediation mode is active; upload dispatch is blocked fail-closed."
            )
        if not reference_id or not reference_id.strip():
            raise UploadDispatchBlocked("A durable upload reference is required for quota admission.")
        if content_class not in {"long", "short"}:
            raise UploadDispatchBlocked("Upload content class must be 'long' or 'short'.")

        project_id = self.project_resolver(self.channel_slug)
        if not project_id or project_id == "unknown":
            raise UploadDispatchBlocked("The channel quota project is unknown; upload blocked fail-closed.")

        # ── Budget: per-project (cuota real - reservados) ──────────
        budget = self.automatic_budget
        if not budget or budget <= 0:
            from config.settings import get_project_automatic_budget_units
            budget = get_project_automatic_budget_units(project_id)

        reservation = self.db.reserve_youtube_quota(
            project_id=project_id,
            quota_day_pt=self.quota_day(),
            operation=UPLOAD_OPERATION,
            content_class=content_class,
            units=UPLOAD_UNITS,
            reference_id=reference_id,
            automatic_budget=budget,
        )
        if not reservation.get("granted"):
            raise UploadDispatchBlocked(
                f"Project quota admission denied: {reservation.get('reason', 'unknown')}"
            )

        request_started = False

        def mark_request_started() -> None:
            nonlocal request_started
            request_started = True

        try:
            result = transport(mark_request_started)
        except BaseException:
            # Once a request may have crossed the process boundary, retain the
            # reservation.  Retrying could otherwise spend another 1,600 units.
            self.db.finalize_youtube_quota_reservation(
                reservation["reservation_id"], consumed=request_started
            )
            raise

        self.db.finalize_youtube_quota_reservation(
            reservation["reservation_id"], consumed=request_started
        )
        return result
