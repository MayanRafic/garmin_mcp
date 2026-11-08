from unittest.mock import Mock

import pytest

from garmin_mcp.workout_delivery import Delivery, running_payload


@pytest.fixture
def setup(tmp_path):
    client = Mock()
    client.upload_workout.return_value = {"workoutId": 17}
    client.get_workout_by_id.return_value = {"workoutName": "TLV 2026-11-06 5km [tlv-2026-11-06]"}
    client.get_scheduled_workouts_by_period.side_effect = [[], [
        {"workoutId": 17, "calendarDate": "2026-11-06", "workoutScheduleId": 99}]]
    return Delivery(client, tmp_path), client, tmp_path


def test_verified_delivery_and_restart_retry_do_not_duplicate(setup):
    delivery, client, path = setup
    first = delivery.publish("tlv-2026-11-06", "2026-11-06", 5)
    assert first["status"] == "scheduled"
    assert "Watch delivery" in first["message"]
    reopened = Delivery(client, path)
    assert reopened.publish("tlv-2026-11-06", "2026-11-06", 5) == first
    client.upload_workout.assert_called_once()
    client.schedule_workout.assert_called_once_with(17, "2026-11-06")
    with pytest.raises(ValueError, match="another workout"):
        reopened.publish("tlv-2026-11-06", "2026-11-06", 6)


@pytest.mark.parametrize("stage", ["upload", "verify_upload", "schedule", "verify_schedule"])
def test_ambiguous_result_is_never_blindly_retried(setup, stage):
    delivery, client, path = setup
    secret = RuntimeError("sensitive transport contents")
    if stage == "upload":
        client.upload_workout.side_effect = secret
    elif stage == "verify_upload":
        client.get_workout_by_id.side_effect = secret
    elif stage == "schedule":
        client.schedule_workout.side_effect = secret
    else:
        client.get_scheduled_workouts_by_period.side_effect = [[], []]
    result = delivery.publish("tlv-2026-11-06", "2026-11-06", 5)
    assert result["status"] == "needs_review"
    assert "sensitive" not in str(result)
    assert Delivery(client, path).publish("tlv-2026-11-06", "2026-11-06", 5) == result
    client.upload_workout.assert_called_once()
    assert client.schedule_workout.call_count <= 1


def test_empty_upload_response_is_not_success(setup):
    delivery, client, _ = setup
    client.upload_workout.return_value = {}
    assert delivery.publish("tlv-2026-11-06", "2026-11-06", 5)["status"] == "needs_review"
    client.schedule_workout.assert_not_called()


def test_undo_only_owned_and_verifies_calendar_and_library(setup):
    delivery, client, _ = setup
    with pytest.raises(ValueError, match="Only workouts"):
        delivery.remove("unrelated")
    delivery.publish("tlv-2026-11-06", "2026-11-06", 5)
    client.get_scheduled_workouts_by_period.side_effect = [[
        {"workout": {"workoutId": 17}, "calendarDate": "2026-11-06", "workoutScheduleId": 99}], []]
    client.get_workouts.return_value = []
    assert delivery.remove("tlv-2026-11-06")["status"] == "removed"
    client.unschedule_workout.assert_called_once_with(99)
    client.delete_workout.assert_called_once_with(17)
    delivery.remove("tlv-2026-11-06")
    client.delete_workout.assert_called_once()


@pytest.mark.parametrize("distance", [0, -1, float("nan"), float("inf"), 100, True])
def test_invalid_distance_never_reaches_garmin(setup, distance):
    delivery, client, _ = setup
    with pytest.raises(ValueError):
        delivery.publish("tlv-2026-11-06", "2026-11-06", distance)
    client.upload_workout.assert_not_called()


def test_payload_distance_includes_warmup_and_has_no_invented_hr_zone():
    payload = running_payload("test", "2026-11-06", 5)
    step = payload["workoutSegments"][0]["workoutSteps"][0]
    assert step["endConditionValue"] == 5000
    assert step["targetType"]["workoutTargetTypeKey"] == "no.target"


def test_workout_flag_cannot_enable_unprotected_stdio(monkeypatch):
    from garmin_mcp.cloud import build_app
    monkeypatch.setenv("GARMIN_ENABLE_WORKOUTS", "true")
    with pytest.raises(ValueError, match="HTTPS OAuth"):
        build_app(Mock())


def test_pace_targets_convert_to_speeds_and_preserve_total_distance():
    payload = running_payload("pace", "2026-11-11", 11, 6.25, 7.5)
    warmup, main, cooldown = payload["workoutSegments"][0]["workoutSteps"]
    assert [s["endConditionValue"] for s in (warmup, main, cooldown)] == [1000, 9000, 1000]
    assert main["targetValueOne"] == pytest.approx(1000 / 375)
    assert main["targetValueTwo"] == pytest.approx(1000 / 450)
    assert main["targetType"]["workoutTargetTypeKey"] == "pace.zone"
    assert all(s["targetType"]["workoutTargetTypeKey"] == "no.target" for s in (warmup, cooldown))


@pytest.mark.parametrize("bounds", [(6.25, None), (None, 7.5), (7.5, 6.25),
                                    (float("nan"), 7.5), (6.25, float("inf")), (True, 7.5)])
def test_invalid_pace_never_uploads(setup, bounds):
    delivery, client, _ = setup
    with pytest.raises(ValueError):
        delivery.publish("pace", "2026-11-11", 11, *bounds)
    client.upload_workout.assert_not_called()


def test_pace_readback_and_restart_retry(setup):
    delivery, client, path = setup
    client.get_workout_by_id.return_value = running_payload("tlv-2026-11-06", "2026-11-06", 5, 6.25, 7.5)
    receipt = delivery.publish("tlv-2026-11-06", "2026-11-06", 5, 6.25, 7.5)
    assert receipt["status"] == "scheduled"
    assert receipt["pace_fast_min_per_km"] == 6.25
    assert Delivery(client, path).publish("tlv-2026-11-06", "2026-11-06", 5, 6.25, 7.5) == receipt
    client.upload_workout.assert_called_once()
    with pytest.raises(ValueError, match="another workout"):
        delivery.publish("tlv-2026-11-06", "2026-11-06", 5, 6.5, 7.5)


@pytest.mark.parametrize("change", ["stripped", "speed", "distance", "edge_target", "time", "reordered"])
def test_unverified_pace_never_schedules(setup, change):
    delivery, client, _ = setup
    observed = running_payload("tlv-2026-11-06", "2026-11-06", 5, 6.25, 7.5)
    steps = observed["workoutSegments"][0]["workoutSteps"]
    if change == "stripped":
        observed.pop("workoutSegments")
    elif change == "speed":
        steps[1]["targetValueOne"] = 6.25  # Wrong units must not pass readback.
    elif change == "distance":
        steps[1]["endConditionValue"] = 5000
    elif change == "edge_target":
        steps[0]["targetType"]["workoutTargetTypeKey"] = "pace.zone"
    elif change == "time":
        steps[1]["endCondition"]["conditionTypeKey"] = "time"
    else:
        steps[0]["stepOrder"] = 3
    client.get_workout_by_id.return_value = observed
    receipt = delivery.publish("tlv-2026-11-06", "2026-11-06", 5, 6.25, 7.5)
    assert receipt["status"] == "needs_review"
    assert receipt["workout_id"] == 17
    client.schedule_workout.assert_not_called()
