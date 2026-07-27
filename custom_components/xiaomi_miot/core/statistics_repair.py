"""Repair false resets in Xiaomi cloud power statistics."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
import logging
import math
from typing import Literal

from homeassistant.components.recorder.statistics import (
    StatisticsRow,
    statistics_during_period,
)
from homeassistant.components.recorder.db_schema import (
    States,
    Statistics,
    StatisticsShortTerm,
)
from homeassistant.components.recorder.tasks import RecorderTask
from homeassistant.components.recorder.util import session_scope
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.recorder import DATA_INSTANCE, get_instance
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.storage import Store
from homeassistant.util import dt

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

REPAIR_VERSION = 2
STORAGE_KEY = f"{DOMAIN}.power_statistics_repair"

DATA_PENDING = "_power_statistics_repair_pending"
DATA_TASK = "_power_statistics_repair_task"
DATA_UNSUB = "_power_statistics_repair_unsub"

PowerStatisticsPeriod = Literal["day", "month"]


@dataclass(frozen=True, slots=True)
class PowerStatisticsAdjustment:
    """A correction to apply to statistics from a point in time."""

    start: datetime
    amount: float


@dataclass(frozen=True, slots=True)
class PowerStatisticsPointRepair:
    """A false zero point and its replacement value."""

    key: int
    replacement: float | str


def _as_finite_non_negative(value) -> float | None:
    """Return a finite non-negative float."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return number


def _period_id(timestamp: float, period: PowerStatisticsPeriod, zone: tzinfo) -> str:
    """Return the local statistics period for an interval end timestamp."""
    local = datetime.fromtimestamp(timestamp, timezone.utc).astimezone(zone)
    if period == "day":
        return local.strftime("%Y-%m-%d")
    return local.strftime("%Y-%m")


def _growth_tolerance(*values: float) -> float:
    """Return a tolerance for statistics rounding differences."""
    return max(0.002, max((abs(value) for value in values), default=0) * 0.002)


def find_false_zero_point_repairs(
    rows: list[dict],
    period: PowerStatisticsPeriod,
    zone: tzinfo,
    *,
    period_offset: float = 0,
    max_gap: float | None = None,
) -> list[PowerStatisticsPointRepair]:
    """Find zero points bounded by valid values in one local period."""
    repairs: list[PowerStatisticsPointRepair] = []
    anchor = None
    zeroes = []
    previous_timestamp = None

    for row in rows:
        key = row.get("id")
        timestamp = _as_finite_non_negative(row.get("timestamp"))
        value = _as_finite_non_negative(row.get("state"))
        if key is None or timestamp is None or value is None:
            anchor = None
            zeroes = []
            previous_timestamp = None
            continue

        point_period = _period_id(timestamp + period_offset, period, zone)
        if (
            previous_timestamp is not None
            and (
                timestamp <= previous_timestamp
                or (
                    max_gap is not None
                    and timestamp - previous_timestamp > max_gap
                )
            )
        ):
            anchor = None
            zeroes = []
        previous_timestamp = timestamp

        if value == 0:
            if anchor is not None and point_period == anchor["period"]:
                zeroes.append((key, point_period))
            else:
                zeroes = []
            continue

        if (
            anchor is not None
            and zeroes
            and point_period == anchor["period"]
            and all(zero_period == point_period for _, zero_period in zeroes)
            and value >= anchor["value"]
        ):
            repairs.extend(
                PowerStatisticsPointRepair(key, anchor["raw_value"])
                for key, _zero_period in zeroes
            )

        anchor = {
            "period": point_period,
            "raw_value": row.get("state"),
            "value": value,
        }
        zeroes = []

    return repairs


def find_false_zero_adjustments(
    rows: list[StatisticsRow],
    period: PowerStatisticsPeriod,
    zone: tzinfo,
) -> list[PowerStatisticsAdjustment]:
    """Find high-confidence false zero resets in ordered statistics rows."""
    adjustments: list[PowerStatisticsAdjustment] = []
    previous = None
    zero_anchor = None

    for row in rows:
        start = _as_finite_non_negative(row.get("start"))
        end = _as_finite_non_negative(row.get("end"))
        state = _as_finite_non_negative(row.get("state"))
        total = _as_finite_non_negative(row.get("sum"))
        if None in (start, end, state, total) or end <= start:
            previous = None
            zero_anchor = None
            continue

        current = {
            "start": start,
            "end": end,
            "state": state,
            "sum": total,
            "period": _period_id(end - 0.000001, period, zone),
        }
        if previous is None:
            previous = current
            continue

        duration = previous["end"] - previous["start"]
        if (
            current["start"] <= previous["start"]
            or current["start"] - previous["end"] > max(1, duration * 0.01)
            or current["period"] != previous["period"]
        ):
            previous = current
            zero_anchor = None
            continue

        if current["state"] == 0 and previous["state"] > 0:
            zero_anchor = zero_anchor or previous
            previous = current
            continue

        if zero_anchor is not None:
            if current["period"] != zero_anchor["period"]:
                zero_anchor = None
            elif current["state"] > 0:
                actual_growth = current["sum"] - zero_anchor["sum"]
                expected_growth = max(
                    current["state"] - zero_anchor["state"],
                    0,
                )
                excess = actual_growth - expected_growth
                tolerance = _growth_tolerance(
                    actual_growth,
                    expected_growth,
                    zero_anchor["state"],
                    current["state"],
                )
                if excess > tolerance:
                    adjustments.append(
                        PowerStatisticsAdjustment(
                            datetime.fromtimestamp(current["start"], timezone.utc),
                            -excess,
                        )
                    )
                zero_anchor = None

        actual_growth = current["sum"] - previous["sum"]
        expected_growth = max(current["state"] - previous["state"], 0)
        excess = actual_growth - expected_growth
        tolerance = _growth_tolerance(
            actual_growth,
            expected_growth,
            previous["state"],
            current["state"],
        )
        if (
            previous["state"] > 0
            and current["state"] > 0
            and excess > tolerance
        ):
            adjustments.append(
                PowerStatisticsAdjustment(
                    datetime.fromtimestamp(current["start"], timezone.utc),
                    -excess,
                )
            )

        previous = current

    return adjustments


def _repair_statistics_state_rows(
    session,
    table,
    metadata_id: int,
    period: PowerStatisticsPeriod,
    zone: tzinfo,
) -> tuple[int, int]:
    """Repair false zero state values in one statistics table."""
    db_rows = (
        session.query(
            table.id,
            table.start_ts,
            table.state,
            table.mean,
            table.min,
            table.max,
        )
        .filter(table.metadata_id == metadata_id)
        .order_by(table.start_ts)
        .all()
    )
    rows = [
        {
            "id": row.id,
            "timestamp": row.start_ts,
            "state": row.state,
        }
        for row in db_rows
    ]
    duration = table.duration.total_seconds()
    repairs = find_false_zero_point_repairs(
        rows,
        period,
        zone,
        period_offset=duration - 0.000001,
        max_gap=duration * 1.01,
    )
    rows_by_id = {row.id: row for row in db_rows}
    updated = 0
    for repair in repairs:
        db_row = rows_by_id[repair.key]
        replacement = float(repair.replacement)
        values = {table.state: replacement}
        for column_name in ("mean", "min", "max"):
            old_value = getattr(db_row, column_name)
            if _as_finite_non_negative(old_value) == 0:
                values[getattr(table, column_name)] = replacement
        updated += (
            session.query(table)
            .filter(table.id == repair.key)
            .update(values, synchronize_session=False)
        )
    return len(db_rows), updated


def _repair_historical_zero_states(
    instance,
    entities: dict[str, tuple[PowerStatisticsPeriod, str]],
    zone: tzinfo,
) -> dict[str, dict]:
    """Repair raw and aggregated false zero state values."""
    result = {}
    with session_scope(session=instance.get_session()) as session:
        entity_ids = set(entities)
        states_metadata = instance.states_meta_manager.get_many(
            entity_ids,
            session,
            True,
        )
        statistics_metadata = instance.statistics_meta_manager.get_many(
            session,
            entity_ids,
        )

        for entity_id, (period, _unit) in entities.items():
            raw_rows = []
            state_metadata_id = states_metadata.get(entity_id)
            if state_metadata_id is not None:
                raw_rows = (
                    session.query(
                        States.state_id,
                        States.last_updated_ts,
                        States.state,
                    )
                    .filter(States.metadata_id == state_metadata_id)
                    .filter(States.last_updated_ts.is_not(None))
                    .order_by(States.last_updated_ts)
                    .all()
                )
            raw_points = [
                {
                    "id": row.state_id,
                    "timestamp": row.last_updated_ts,
                    "state": row.state,
                }
                for row in raw_rows
            ]
            raw_repairs = find_false_zero_point_repairs(
                raw_points,
                period,
                zone,
            )
            raw_updated = 0
            for repair in raw_repairs:
                raw_updated += (
                    session.query(States)
                    .filter(States.state_id == repair.key)
                    .update(
                        {States.state: str(repair.replacement)},
                        synchronize_session=False,
                    )
                )

            statistics_rows = 0
            statistics_updated = 0
            if metadata := statistics_metadata.get(entity_id):
                metadata_id = metadata[0]
                for table in (Statistics, StatisticsShortTerm):
                    scanned, updated = _repair_statistics_state_rows(
                        session,
                        table,
                        metadata_id,
                        period,
                        zone,
                    )
                    statistics_rows += scanned
                    statistics_updated += updated

            result[entity_id] = {
                "raw_rows": len(raw_rows),
                "raw_state_repairs": raw_updated,
                "statistics_rows": statistics_rows,
                "statistics_state_repairs": statistics_updated,
                "verified": (
                    raw_updated == len(raw_repairs)
                    and statistics_updated
                    <= statistics_rows
                ),
            }
    return result


@dataclass(slots=True)
class RepairPowerStatisticsStatesTask(RecorderTask):
    """Recorder task which repairs historical false zero state points."""

    entities: dict[str, tuple[PowerStatisticsPeriod, str]]
    zone: tzinfo
    result: dict[str, dict] | None = None

    def run(self, instance) -> None:
        """Run the repair in Recorder's serialized task queue."""
        self.result = _repair_historical_zero_states(
            instance,
            self.entities,
            self.zone,
        )


def _statistics_rows(
    hass: HomeAssistant,
    entity_ids: set[str],
    short_term_start: datetime,
) -> tuple[dict[str, list[StatisticsRow]], dict[str, list[StatisticsRow]]]:
    """Load long- and short-term statistics for repair."""
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    types = {"state", "sum"}
    hourly = statistics_during_period(
        hass,
        epoch,
        short_term_start,
        entity_ids,
        "hour",
        None,
        types,
    )
    short_term = statistics_during_period(
        hass,
        short_term_start,
        None,
        entity_ids,
        "5minute",
        None,
        types,
    )
    cutoff = short_term_start.timestamp()
    hourly = {
        entity_id: [
            row
            for row in rows
            if (end := _as_finite_non_negative(row.get("end"))) is not None
            and end <= cutoff
        ]
        for entity_id, rows in hourly.items()
    }
    return hourly, short_term


async def async_repair_power_statistics(
    hass: HomeAssistant,
    entities: dict[str, tuple[PowerStatisticsPeriod, str]],
) -> None:
    """Repair historical false zero resets once for each entity."""
    if not entities or DATA_INSTANCE not in hass.data:
        return

    recorder = get_instance(hass)
    await recorder.async_recorder_ready.wait()

    store = Store(hass, 1, STORAGE_KEY)
    stored = await store.async_load() or {}
    if stored.get("repair_version") != REPAIR_VERSION:
        stored = {
            "repair_version": REPAIR_VERSION,
            "entities": {},
        }
    completed = stored.setdefault("entities", {})
    pending = {
        entity_id: spec
        for entity_id, spec in entities.items()
        if entity_id not in completed
    }
    if not pending:
        return

    keep_days = max(int(getattr(recorder, "keep_days", 10)), 1)
    short_term_start = (
        dt.utcnow() - timedelta(days=max(keep_days - 1, 0))
    ).replace(minute=0, second=0, microsecond=0)
    zone = dt.get_time_zone(hass.config.time_zone) or timezone.utc

    def load_rows():
        return _statistics_rows(hass, set(pending), short_term_start)

    hourly, short_term = await recorder.async_add_executor_job(load_rows)
    adjustments_by_entity: dict[str, list[PowerStatisticsAdjustment]] = {}
    for entity_id, (period, _unit) in pending.items():
        adjustments_by_entity[entity_id] = [
            *find_false_zero_adjustments(hourly.get(entity_id, []), period, zone),
            *find_false_zero_adjustments(
                short_term.get(entity_id, []),
                period,
                zone,
            ),
        ]

    state_repair_task = RepairPowerStatisticsStatesTask(pending, zone)
    recorder.queue_task(state_repair_task)
    for entity_id, adjustments in adjustments_by_entity.items():
        unit = pending[entity_id][1]
        for adjustment in adjustments:
            recorder.async_adjust_statistics(
                entity_id,
                adjustment.start,
                adjustment.amount,
                unit,
            )

    await recorder.async_block_till_done()
    hourly, short_term = await recorder.async_add_executor_job(load_rows)
    state_results = state_repair_task.result or {}

    now = dt.utcnow().isoformat()
    for entity_id, (period, unit) in pending.items():
        remaining = [
            *find_false_zero_adjustments(hourly.get(entity_id, []), period, zone),
            *find_false_zero_adjustments(
                short_term.get(entity_id, []),
                period,
                zone,
            ),
        ]
        if remaining:
            _LOGGER.error(
                "Historical power statistics repair could not be verified for %s",
                entity_id,
            )
            continue

        state_result = state_results.get(entity_id)
        if (
            not state_result
            or not state_result["verified"]
            or state_result["statistics_rows"] == 0
        ):
            _LOGGER.error(
                "Historical power statistics state repair could not be "
                "verified for %s",
                entity_id,
            )
            continue

        adjustments = adjustments_by_entity[entity_id]
        completed[entity_id] = {
            "completed_at": now,
            "period": period,
            "adjustments": len(adjustments),
            "raw_state_repairs": state_result["raw_state_repairs"],
            "statistics_state_repairs": state_result[
                "statistics_state_repairs"
            ],
            "total_adjustment": round(
                sum(adjustment.amount for adjustment in adjustments),
                6,
            ),
            "unit": unit,
        }
        repair_count = (
            len(adjustments)
            + state_result["raw_state_repairs"]
            + state_result["statistics_state_repairs"]
        )
        if repair_count:
            _LOGGER.warning(
                "Repaired historical false zero data for %s: "
                "sum_adjustments=%s (%s %s), raw_states=%s, "
                "statistics_states=%s",
                entity_id,
                len(adjustments),
                completed[entity_id]["total_adjustment"],
                unit,
                state_result["raw_state_repairs"],
                state_result["statistics_state_repairs"],
            )

    await store.async_save(stored)


async def _async_repair_pending(hass: HomeAssistant) -> None:
    """Repair all entities queued during platform setup."""
    data = hass.data[DOMAIN]
    try:
        while pending := data.get(DATA_PENDING):
            entities = dict(pending)
            pending.clear()
            await async_repair_power_statistics(hass, entities)
    finally:
        data.pop(DATA_TASK, None)
        data.pop(DATA_UNSUB, None)


@callback
def async_schedule_power_statistics_repair(
    hass: HomeAssistant,
    entity_id: str,
    period: PowerStatisticsPeriod,
    unit: str | None,
) -> None:
    """Schedule one-time repair after Home Assistant has started."""
    if not entity_id or not unit:
        return

    data = hass.data[DOMAIN]
    pending = data.setdefault(DATA_PENDING, {})
    pending[entity_id] = (period, unit)
    if data.get(DATA_TASK) or data.get(DATA_UNSUB):
        return

    @callback
    def start_repair(_hass: HomeAssistant) -> None:
        data.pop(DATA_UNSUB, None)
        data[DATA_TASK] = hass.async_create_task(
            _async_repair_pending(hass),
            f"{DOMAIN} power statistics repair",
        )

    data[DATA_UNSUB] = async_at_started(hass, start_repair)
