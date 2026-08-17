"""Unit tests for the Swarm Office Visualizer helpers."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from swarmer.routers.api_client import APIError
from swarmer.routers.office import (
    _PHASE_ACTIVITY,
    _build_offices,
    _format_next_run,
    _session_card,
)


def test_format_next_run_iso_strings_keep_utc_label():
    """Raw ISO strings with +00:00 or Z both keep a UTC label after truncation."""
    assert _format_next_run("2026-07-30T09:00:00+00:00") == "2026-07-30 09:00 UTC"
    assert _format_next_run("2026-07-30T09:00:00Z") == "2026-07-30 09:00 UTC"


def test_format_next_run_datetime_object():
    """Datetime values use the strftime tooltip format."""
    value = datetime(2026, 7, 30, 9, 0, tzinfo=timezone.utc)
    assert _format_next_run(value) == "Jul 30, 2026 09:00 UTC"
    assert _format_next_run(None) == ""
    assert _format_next_run("") == ""


def test_session_card_maps_running_phase():
    """Running sessions map to typing activity and Running label."""
    card = _session_card(
        {
            "id": 7,
            "name": "cve-scan",
            "phase": "running",
            "mode": "prompt",
            "is_active": True,
            "run_duration": "3m",
            "status_detail": "working",
        }
    )
    assert card["activity"] == "typing"
    assert card["label"] == "Running"
    assert card["is_active"] is True
    assert card["name"] == "cve-scan"
    assert card["in_restroom"] is False
    assert card["look"] == "look-1"  # 7 % 6
    assert card["schedules"] == []
    assert card["schedule_count"] == 0


def test_session_card_includes_schedule_summaries():
    """Schedule rows become hover-tooltip summaries with labels and next run."""
    next_run = datetime(2026, 7, 30, 9, 0, tzinfo=timezone.utc)
    card = _session_card(
        {
            "id": 9,
            "name": "nightly",
            "phase": "idle",
            "schedules": [
                {
                    "cron_schedule": "0 0 * * *",
                    "label": "",
                    "enabled": True,
                    "cron_next_run": next_run,
                },
                {
                    "cron_schedule": "0 * * * *",
                    "label": "Hourly sweep",
                    "enabled": False,
                    "cron_next_run": None,
                },
            ],
        }
    )
    assert card["schedule_count"] == 1
    assert card["schedules"][0]["label"] == "Daily midnight"
    assert "Jul 30" in card["schedules"][0]["next_run"]
    assert card["schedules"][1]["label"] == "Hourly sweep"
    assert card["schedules"][1]["enabled"] is False


def test_session_card_succeeded_goes_to_restroom_for_coffee():
    """Succeeded sessions go to the break room with coffee activity."""
    card = _session_card({"id": 3, "phase": "succeeded", "name": "done-job"})
    assert card["activity"] == "coffee"
    assert card["label"] == "Succeeded"
    assert card["in_restroom"] is True


def test_session_card_failed_is_crying():
    """Failed sessions stay at the desk with crying activity."""
    card = _session_card({"id": 4, "phase": "failed"})
    assert card["activity"] == "crying"
    assert card["in_restroom"] is False


def test_session_card_defaults_unknown_phase_to_idle():
    """Unknown phases fall back to idle activity and default fields."""
    card = _session_card({"id": 1, "phase": "mystery"})
    assert card["activity"] == "idle"
    assert card["name"] == "session-1"
    assert card["mode"] == "prompt"


def test_all_known_phases_have_activities():
    """Every session phase has a dedicated office activity mapping."""
    expected = {
        "idle": "idle",
        "queued": "waiting",
        "pending": "walking",
        "running": "typing",
        "succeeded": "coffee",
        "failed": "crying",
        "stopped": "sleeping",
    }
    assert _PHASE_ACTIVITY == expected
    for phase, activity in expected.items():
        assert _session_card({"id": 2, "phase": phase})["activity"] == activity


def test_session_card_look_rotates_by_id():
    """Palette look class rotates by session id modulo look count."""
    assert _session_card({"id": 0, "phase": "idle"})["look"] == "look-0"
    assert _session_card({"id": 6, "phase": "idle"})["look"] == "look-0"
    assert _session_card({"id": 5, "phase": "idle"})["look"] == "look-5"


@pytest.mark.asyncio
async def test_build_offices_isolates_list_sessions_api_error():
    """One workspace list_sessions failure must not abort the whole floor."""
    ws_ok = {"id": 1, "display_name": "ok"}
    ws_bad = {"id": 2, "display_name": "bad"}

    async def _list_sessions(wid: int):
        """Return sessions for workspace 1; raise APIError for others."""
        if wid == 1:
            return [{"id": 10, "phase": "running", "name": "alive", "is_active": True}]
        raise APIError(500, "boom")

    api = AsyncMock()
    api.list_workspaces = AsyncMock(return_value=[ws_ok, ws_bad])
    api.list_sessions = AsyncMock(side_effect=_list_sessions)

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=api)
    ctx.__aexit__ = AsyncMock(return_value=None)

    request = MagicMock()
    with patch("swarmer.routers.office.get_api_client", return_value=ctx):
        offices = await _build_offices(request)

    assert len(offices) == 2
    by_id = {o["workspace"]["id"]: o for o in offices}
    assert by_id[1]["unavailable"] is False
    assert by_id[1]["total_count"] == 1
    assert by_id[1]["sessions"][0]["name"] == "alive"
    assert by_id[2]["unavailable"] is True
    assert by_id[2]["sessions"] == []
    assert by_id[2]["total_count"] == 0
