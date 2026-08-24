"""Console routes — Swarm Office Visualizer.

Maps workspaces to offices and sessions to animated human characters
so operators can watch swarm activity in near real time.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from swarmer.deps import require_auth
from swarmer.models.session import CRON_PRESETS
from swarmer.routers.api_client import APIError, get_api_client

log = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory="swarmer/templates")

# Cap concurrent list_sessions calls on the global office floor so a large
# workspace count does not fan out into an unbounded N+1 storm.
_SESSION_FETCH_CONCURRENCY = 5

# phase → visual activity used by the office scene CSS
_PHASE_ACTIVITY: dict[str, str] = {
    "idle": "idle",
    "queued": "waiting",
    "pending": "walking",
    "running": "typing",
    "succeeded": "coffee",
    "failed": "crying",
    "stopped": "sleeping",
}

# Labels match session status badges so the office view stays unambiguous.
_PHASE_LABEL: dict[str, str] = {
    "idle": "Idle",
    "queued": "Queued",
    "pending": "Pending",
    "running": "Running",
    "succeeded": "Succeeded",
    "failed": "Failed",
    "stopped": "Stopped",
}

# Predefined palette class suffixes (desk-station--look-N) defined in office.css.
# Avoid inline style=/CSS variables so strict CSP can disallow unsafe-inline.
_LOOK_COUNT = 6


def _cron_label(cron_expr: str) -> str:
    """Human-readable cron label when it matches a known preset."""
    if not cron_expr:
        return ""
    return CRON_PRESETS.get(cron_expr, cron_expr)


def _format_next_run(value: object) -> str:
    """Format a schedule next-run timestamp for the hover tooltip."""
    if value is None or value == "":
        return ""
    if hasattr(value, "strftime"):
        return value.strftime("%b %d, %Y %H:%M UTC")
    text = str(value)
    # ISO strings from the API client before/without datetime parsing
    if "T" in text:
        text = text.replace("T", " ").replace("+00:00", " UTC").rstrip("Z")
        truncated = text[:16]
        return truncated + ("" if "UTC" in truncated else " UTC")
    return text


def _schedule_summaries(session: dict) -> list[dict]:
    """Build compact schedule rows for character hover tooltips."""
    raw = session.get("schedules") or []
    summaries: list[dict] = []
    for sched in raw:
        cron = sched.get("cron_schedule") or ""
        label = (sched.get("label") or "").strip() or _cron_label(cron)
        summaries.append(
            {
                "label": label,
                "cron": cron,
                "enabled": bool(sched.get("enabled", True)),
                "next_run": _format_next_run(sched.get("cron_next_run")),
            }
        )
    # Fallback to deprecated session-level cron if no schedule rows exist
    if not summaries and session.get("cron_schedule"):
        cron = session["cron_schedule"]
        summaries.append(
            {
                "label": session.get("cron_label") or _cron_label(cron),
                "cron": cron,
                "enabled": True,
                "next_run": "",
            }
        )
    return summaries


def _session_card(session: dict) -> dict:
    """Normalize a session dict into office-scene card data."""
    phase = session.get("phase") or "idle"
    sid = int(session["id"])
    schedules = _schedule_summaries(session)
    enabled = [s for s in schedules if s["enabled"]]
    return {
        "id": sid,
        "name": session.get("name") or f"session-{sid}",
        "phase": phase,
        "mode": session.get("mode") or "prompt",
        "is_active": bool(session.get("is_active")),
        "run_duration": session.get("run_duration") or "",
        "status_detail": session.get("status_detail") or "",
        "activity": _PHASE_ACTIVITY.get(phase, "idle"),
        "label": _PHASE_LABEL.get(phase, phase.title()),
        "look": f"look-{sid % _LOOK_COUNT}",
        "in_restroom": phase == "succeeded",
        "schedules": schedules,
        "schedule_count": len(enabled),
    }


async def _build_offices(request: Request, ws_id: int | None = None) -> list[dict]:
    """Load workspaces + sessions and shape them for the office scene.

    Session lists for the global floor are fetched with bounded concurrency
    so many workspaces do not each wait on a serial N+1 round-trip.
    """
    async with get_api_client(request) as api:
        if ws_id is not None:
            ws = await api.get_workspace(ws_id)
            workspaces = [ws]
        else:
            workspaces = await api.list_workspaces()

        if not workspaces:
            return []

        sem = asyncio.Semaphore(_SESSION_FETCH_CONCURRENCY)

        async def _sessions_for(ws: dict) -> tuple[dict, list, bool]:
            """Return (workspace, sessions, unavailable).

            Isolate per-workspace list_sessions failures so one bad workspace
            does not abort the whole office floor / HTMX poll.
            """
            async with sem:
                try:
                    sessions = await api.list_sessions(ws["id"])
                    return ws, sessions, False
                except APIError as exc:
                    # Log status only — avoid workspace IDs and API detail strings.
                    log.warning(
                        "office: list_sessions failed (HTTP %s)",
                        exc.status_code,
                    )
                    return ws, [], True

        loaded = await asyncio.gather(*[_sessions_for(ws) for ws in workspaces])

        offices: list[dict] = []
        for ws, sessions, unavailable in loaded:
            cards = [_session_card(s) for s in sessions]
            workers = [c for c in cards if not c["in_restroom"]]
            resters = [c for c in cards if c["in_restroom"]]
            active = sum(1 for c in cards if c["is_active"])
            offices.append(
                {
                    "workspace": ws,
                    "sessions": cards,
                    "workers": workers,
                    "resters": resters,
                    "active_count": active,
                    "total_count": len(cards),
                    "unavailable": unavailable,
                }
            )
    return offices


@router.get("/office", dependencies=[Depends(require_auth)])
async def office_index(request: Request) -> HTMLResponse:
    """Global office floor — one room per workspace."""
    offices = await _build_offices(request)
    return templates.TemplateResponse(
        request,
        "offices/index.html",
        {
            "offices": offices,
            "scope": "all",
            "poll_url": "/office/scene",
            "ws": None,
        },
    )


@router.get(
    "/office/scene",
    dependencies=[Depends(require_auth)],
    response_class=HTMLResponse,
)
async def office_scene(request: Request) -> HTMLResponse:
    """HTMX partial — polled periodically for live animation state."""
    offices = await _build_offices(request)
    return templates.TemplateResponse(
        request,
        "offices/_scene.html",
        {"offices": offices, "scope": "all", "ws": None},
    )


@router.get(
    "/workspaces/{ws_id}/office",
    dependencies=[Depends(require_auth)],
)
async def workspace_office(request: Request, ws_id: int) -> HTMLResponse:
    """Single-workspace office view."""
    try:
        offices = await _build_offices(request, ws_id=ws_id)
    except APIError:
        return RedirectResponse(url="/workspaces", status_code=302)
    ws = offices[0]["workspace"] if offices else None
    return templates.TemplateResponse(
        request,
        "offices/index.html",
        {
            "offices": offices,
            "scope": "workspace",
            "poll_url": f"/workspaces/{ws_id}/office/scene",
            "ws": ws,
        },
    )


@router.get(
    "/workspaces/{ws_id}/office/scene",
    dependencies=[Depends(require_auth)],
    response_class=HTMLResponse,
)
async def workspace_office_scene(request: Request, ws_id: int) -> HTMLResponse:
    """HTMX partial for a single-workspace office scene."""
    try:
        offices = await _build_offices(request, ws_id=ws_id)
    except APIError:
        return HTMLResponse("")
    ws = offices[0]["workspace"] if offices else None
    return templates.TemplateResponse(
        request,
        "offices/_scene.html",
        {"offices": offices, "scope": "workspace", "ws": ws},
    )
