import asyncio
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from homeassistant.helpers.recorder import DATA_INSTANCE

from custom_components.xiaomi_miot.core import statistics_repair
from custom_components.xiaomi_miot.core.statistics_repair import (
    find_false_zero_adjustments,
)


def statistic_rows(start, states, sums, interval=timedelta(hours=1)):
    rows = []
    for index, (state, total) in enumerate(zip(states, sums, strict=True)):
        row_start = start + interval * index
        rows.append({
            "start": row_start.timestamp(),
            "end": (row_start + interval).timestamp(),
            "state": state,
            "sum": total,
        })
    return rows


def adjustment_values(rows, period="day", zone=timezone.utc):
    return [
        (adjustment.start, pytest.approx(adjustment.amount))
        for adjustment in find_false_zero_adjustments(rows, period, zone)
    ]


def test_detects_false_zero_hidden_inside_statistics_interval():
    start = datetime(2026, 7, 27, 8, tzinfo=timezone.utc)
    rows = statistic_rows(start, [17.5, 17.8], [17.5, 35.3])

    assert adjustment_values(rows) == [
        (start + timedelta(hours=1), pytest.approx(-17.5)),
    ]


@pytest.mark.parametrize(
    ("recovered", "expected"),
    [
        (17.8, -17.5),
        (16.8, -16.8),
    ],
)
def test_detects_observed_zero_then_recovery(recovered, expected):
    start = datetime(2026, 7, 27, 8, tzinfo=timezone.utc)
    rows = statistic_rows(
        start,
        [17.5, 0, recovered],
        [17.5, 17.5, 17.5 + recovered],
    )

    assert adjustment_values(rows) == [
        (start + timedelta(hours=2), pytest.approx(expected)),
    ]


def test_ignores_normal_growth_and_small_rounding_differences():
    start = datetime(2026, 7, 27, 8, tzinfo=timezone.utc)
    rows = statistic_rows(
        start,
        [17.5, 17.8, 18.0],
        [100, 100.3004, 100.5004],
    )

    assert adjustment_values(rows) == []


def test_ignores_legitimate_daily_reset():
    start = datetime(2026, 7, 26, 15, tzinfo=timezone.utc)
    rows = statistic_rows(start, [22.4, 0.2], [100, 100.2])

    assert adjustment_values(rows, zone=ZoneInfo("Asia/Shanghai")) == []


def test_ignores_legitimate_monthly_reset():
    start = datetime(2026, 7, 31, 23, tzinfo=timezone.utc)
    rows = statistic_rows(start, [450, 1.2], [900, 901.2])

    assert adjustment_values(rows, "month") == []


def test_ignores_non_hour_aligned_local_period_boundary():
    start = datetime(2026, 7, 26, 17, tzinfo=timezone.utc)
    rows = statistic_rows(start, [22.4, 0.2], [100, 100.7])

    assert adjustment_values(rows, zone=ZoneInfo("Asia/Kolkata")) == []


def test_ignores_gaps_and_invalid_rows():
    start = datetime(2026, 7, 27, 8, tzinfo=timezone.utc)
    rows = statistic_rows(start, [17.5, 17.8], [17.5, 35.3])
    rows[1]["start"] += 3600
    rows[1]["end"] += 3600
    rows.append({
        "start": (start + timedelta(hours=4)).timestamp(),
        "end": (start + timedelta(hours=5)).timestamp(),
        "state": float("nan"),
        "sum": 50,
    })

    assert adjustment_values(rows) == []


def test_detection_is_idempotent_after_adjustment():
    start = datetime(2026, 7, 27, 8, tzinfo=timezone.utc)
    rows = statistic_rows(start, [17.5, 17.8], [17.5, 35.3])
    adjustments = find_false_zero_adjustments(rows, "day", timezone.utc)
    assert len(adjustments) == 1

    adjustment_start = adjustments[0].start.timestamp()
    for row in rows:
        if row["start"] >= adjustment_start:
            row["sum"] += adjustments[0].amount

    assert find_false_zero_adjustments(rows, "day", timezone.utc) == []


def test_detects_multiple_independent_false_resets():
    start = datetime(2026, 7, 27, 8, tzinfo=timezone.utc)
    rows = statistic_rows(
        start,
        [10, 10.5, 0, 11, 11.5],
        [10, 10.5, 10.5, 21.5, 33],
    )

    assert adjustment_values(rows) == [
        (start + timedelta(hours=3), pytest.approx(-10.5)),
        (start + timedelta(hours=4), pytest.approx(-11)),
    ]


async def test_async_repair_adjusts_and_persists_completion(hass, monkeypatch):
    start = datetime(2026, 7, 27, 8, tzinfo=timezone.utc)
    rows = statistic_rows(start, [17.5, 17.8], [17.5, 35.3])
    stored = {}

    class FakeStore:
        def __init__(self, *_args):
            pass

        async def async_load(self):
            return stored or None

        async def async_save(self, data):
            stored.clear()
            stored.update(data)

    class FakeRecorder:
        keep_days = 10

        def __init__(self):
            self.async_recorder_ready = asyncio.Event()
            self.async_recorder_ready.set()
            self.adjustments = []

        async def async_add_executor_job(self, func):
            return func()

        def async_adjust_statistics(
            self,
            entity_id,
            adjustment_start,
            amount,
            unit,
        ):
            self.adjustments.append(
                (entity_id, adjustment_start, amount, unit)
            )
            for row in rows:
                if row["start"] >= adjustment_start.timestamp():
                    row["sum"] += amount

        async def async_block_till_done(self):
            return None

    recorder = FakeRecorder()
    hass.data[DATA_INSTANCE] = recorder
    hass.config.time_zone = "UTC"
    monkeypatch.setattr(statistics_repair, "Store", FakeStore)
    monkeypatch.setattr(statistics_repair, "get_instance", lambda _hass: recorder)
    monkeypatch.setattr(
        statistics_repair,
        "_statistics_rows",
        lambda *_args: ({"sensor.energy_today": rows}, {}),
    )

    entities = {"sensor.energy_today": ("day", "kWh")}
    await statistics_repair.async_repair_power_statistics(hass, entities)

    assert recorder.adjustments == [
        (
            "sensor.energy_today",
            start + timedelta(hours=1),
            pytest.approx(-17.5),
            "kWh",
        )
    ]
    assert stored["entities"]["sensor.energy_today"]["adjustments"] == 1
    assert stored["entities"]["sensor.energy_today"]["total_adjustment"] == -17.5

    await statistics_repair.async_repair_power_statistics(hass, entities)
    assert len(recorder.adjustments) == 1
