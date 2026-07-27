import asyncio
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from homeassistant.components.recorder.db_schema import (
    Base,
    States,
    StatesMeta,
    StatisticsMeta,
    StatisticsShortTerm,
)
from homeassistant.helpers.recorder import DATA_INSTANCE
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from custom_components.xiaomi_miot.core import statistics_repair
from custom_components.xiaomi_miot.core.statistics_repair import (
    _repair_historical_zero_states,
    find_false_zero_adjustments,
    find_false_zero_point_repairs,
    power_statistics_period,
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


@pytest.mark.parametrize(
    ("attribute", "period"),
    [
        ("power_cost_today", "day"),
        ("sensor.power_cost_today", "day"),
        ("sensor.power_cost_today_2", "day"),
        ("power_cost_month", "month"),
        ("sensor.power_cost_month_3", "month"),
        ("sensor.other_energy", None),
    ],
)
def test_power_statistics_period_accepts_entity_attribute_names(
    attribute,
    period,
):
    assert power_statistics_period(attribute) == period


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


def test_detects_hidden_reset_after_growth_inside_statistics_interval():
    start = datetime(2026, 7, 27, 8, tzinfo=timezone.utc)
    rows = statistic_rows(start, [17.0, 18.0], [100, 118.5])

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


def test_ignores_daily_reset_first_observed_after_midnight():
    start = datetime(2026, 7, 26, 16, tzinfo=timezone.utc)
    rows = statistic_rows(
        start,
        [15.9, 0.1, 0.1, 0.3],
        [100, 100.1, 100.1, 100.3],
        interval=timedelta(minutes=5),
    )

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


def point_repairs(rows, period="day", zone=timezone.utc, **kwargs):
    return [
        (repair.key, repair.replacement)
        for repair in find_false_zero_point_repairs(
            rows,
            period,
            zone,
            **kwargs,
        )
    ]


def test_repairs_raw_zero_points_bounded_in_same_period():
    start = datetime(2026, 7, 27, 8, tzinfo=timezone.utc).timestamp()
    rows = [
        {"id": 1, "timestamp": start, "state": "17.5"},
        {"id": 2, "timestamp": start + 600, "state": "0"},
        {"id": 3, "timestamp": start + 1200, "state": "0.0"},
        {"id": 4, "timestamp": start + 1800, "state": "17.8"},
    ]

    assert point_repairs(rows) == [(2, "17.5"), (3, "17.5")]


def test_does_not_repair_zero_across_period_or_lower_recovery():
    midnight = datetime(2026, 7, 27, 0, tzinfo=timezone.utc).timestamp()
    rows = [
        {"id": 1, "timestamp": midnight - 600, "state": 22.4},
        {"id": 2, "timestamp": midnight, "state": 0},
        {"id": 3, "timestamp": midnight + 600, "state": 0.2},
        {"id": 4, "timestamp": midnight + 1200, "state": 0},
        {"id": 5, "timestamp": midnight + 1800, "state": 0.1},
    ]

    assert point_repairs(rows) == []


def test_repairs_statistics_zero_using_interval_end_period():
    midnight = datetime(2026, 7, 27, 0, tzinfo=timezone.utc).timestamp()
    rows = [
        {"id": 1, "timestamp": midnight - 600, "state": 22.4},
        {"id": 2, "timestamp": midnight - 300, "state": 0},
        {"id": 3, "timestamp": midnight, "state": 0.2},
        {"id": 4, "timestamp": midnight + 300, "state": 0},
        {"id": 5, "timestamp": midnight + 600, "state": 0.3},
    ]

    assert point_repairs(
        rows,
        period_offset=300 - 0.000001,
        max_gap=303,
    ) == [(4, 0.2)]


def test_repairs_raw_and_statistics_zero_rows_in_recorder_database():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    make_session = sessionmaker(bind=engine)
    session = make_session()
    session.add(StatesMeta(metadata_id=1, entity_id="sensor.energy_today"))
    session.add(
        StatisticsMeta(
            id=1,
            statistic_id="sensor.energy_today",
            source="recorder",
            unit_of_measurement="kWh",
            has_sum=True,
        )
    )
    start = datetime(2026, 7, 27, 8, tzinfo=timezone.utc).timestamp()
    for index, value in enumerate(("17.5", "0", "17.8")):
        session.add(
            States(
                metadata_id=1,
                state=value,
                last_updated_ts=start + index * 600,
            )
        )
    for index, value in enumerate((17.5, 0, 17.8)):
        session.add(
            StatisticsShortTerm(
                metadata_id=1,
                start_ts=start + index * 300,
                state=value,
                min=value,
                sum=100 + index,
            )
        )
    session.commit()
    session.close()

    class FakeStatesMetadata:
        def get_many(self, entity_ids, _session, _from_recorder):
            return dict.fromkeys(entity_ids, 1)

    class FakeStatisticsMetadata:
        def get_many(self, _session, statistic_ids):
            return {
                statistic_id: (1, {})
                for statistic_id in statistic_ids
            }

    class FakeInstance:
        states_meta_manager = FakeStatesMetadata()
        statistics_meta_manager = FakeStatisticsMetadata()

        def get_session(self):
            return make_session()

    result = _repair_historical_zero_states(
        FakeInstance(),
        {"sensor.energy_today": ("day", "kWh")},
        timezone.utc,
    )

    assert result["sensor.energy_today"] == {
        "raw_rows": 3,
        "raw_state_repairs": 1,
        "statistics_rows": 3,
        "statistics_state_repairs": 1,
        "verified": True,
    }
    session = make_session()
    assert [
        state
        for state, in session.query(States.state).order_by(States.state_id)
    ] == ["17.5", "17.5", "17.8"]
    assert [
        (state, minimum)
        for state, minimum in session.query(
            StatisticsShortTerm.state,
            StatisticsShortTerm.min,
        ).order_by(StatisticsShortTerm.id)
    ] == [(17.5, 17.5), (17.5, 17.5), (17.8, 17.8)]
    session.close()


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

        def queue_task(self, task):
            task.result = {
                "sensor.energy_today": {
                    "raw_rows": 3,
                    "raw_state_repairs": 1,
                    "statistics_rows": 2,
                    "statistics_state_repairs": 1,
                    "verified": True,
                }
            }

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
    assert stored["entities"]["sensor.energy_today"]["raw_state_repairs"] == 1
    assert (
        stored["entities"]["sensor.energy_today"]["statistics_state_repairs"]
        == 1
    )
    assert stored["entities"]["sensor.energy_today"]["total_adjustment"] == -17.5

    await statistics_repair.async_repair_power_statistics(hass, entities)
    assert len(recorder.adjustments) == 1
