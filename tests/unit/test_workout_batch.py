from unittest.mock import Mock

import pytest

from garmin_mcp.workout_batch import run_batch
from garmin_mcp.workout_delivery import running_payload


@pytest.fixture
def allowed(monkeypatch):
    monkeypatch.setenv("GARMIN_ENABLE_WORKOUTS", "true")
    monkeypatch.setenv("GARMIN_TRANSPORT", "https")


def test_whole_batch_is_validated_before_upload(allowed, tmp_path):
    client = Mock()
    good = dict(session_key="one", calendar_date="2026-11-11", distance_km=11)
    for invalid in [dict(good, distance_km=100), good, dict(good, unexpected="value")]:
        with pytest.raises(ValueError):
            run_batch([good, invalid], client, tmp_path)
    client.upload_workout.assert_not_called()


def test_batch_rechecks_calendar_and_does_not_duplicate_on_restart(allowed, tmp_path):
    row = dict(session_key="one", calendar_date="2026-11-11", distance_km=11,
               pace_fast_min_per_km=6.25, pace_slow_min_per_km=7.5)
    client = Mock()
    client.upload_workout.return_value = {"workoutId": 17}
    client.get_workout_by_id.return_value = running_payload("one", "2026-11-11", 11, 6.25, 7.5)
    client.get_scheduled_workouts_by_period.return_value = [{"workoutId": 17, "calendarDate": "2026-11-11"}]
    assert run_batch([row], client, tmp_path)[0]["currently_scheduled"] is True
    assert run_batch([row], client, tmp_path)[0]["status"] == "scheduled"
    client.upload_workout.assert_called_once()
    client.schedule_workout.assert_not_called()


def test_batch_requires_enabled_https(monkeypatch, tmp_path):
    monkeypatch.setenv("GARMIN_ENABLE_WORKOUTS", "true")
    monkeypatch.setenv("GARMIN_TRANSPORT", "stdio")
    client = Mock()
    with pytest.raises(ValueError, match="HTTPS"):
        run_batch([], client, tmp_path)
    client.upload_workout.assert_not_called()
