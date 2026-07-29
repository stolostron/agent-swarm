"""Unit tests for the Swarm Office Visualizer helpers."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datetime import datetime, timezone

from swarmer.routers.office import _PHASE_ACTIVITY, _session_card


def test_session_card_maps_running_phase():
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
    card = _session_card({"id": 3, "phase": "succeeded", "name": "done-job"})
    assert card["activity"] == "coffee"
    assert card["label"] == "Succeeded"
    assert card["in_restroom"] is True


def test_session_card_failed_is_crying():
    card = _session_card({"id": 4, "phase": "failed"})
    assert card["activity"] == "crying"
    assert card["in_restroom"] is False


def test_session_card_defaults_unknown_phase_to_idle():
    card = _session_card({"id": 1, "phase": "mystery"})
    assert card["activity"] == "idle"
    assert card["name"] == "session-1"
    assert card["mode"] == "prompt"


def test_all_known_phases_have_activities():
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
    assert _session_card({"id": 0, "phase": "idle"})["look"] == "look-0"
    assert _session_card({"id": 6, "phase": "idle"})["look"] == "look-0"
    assert _session_card({"id": 5, "phase": "idle"})["look"] == "look-5"
