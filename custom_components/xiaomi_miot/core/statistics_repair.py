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
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.recorder import DATA_INSTANCE, get_instance
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.storage import Store
from homeassistant.util import dt

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

REPAIR_VERSION = 1
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


def _matches_false_zero_reset(excess: float, before: float, after: float) -> bool:
    """Return whether excess growth matches a reset through zero."""
    reset_growth = min(before, after)
    tolerance = max(0.002, reset_growth * 0.002)
    return reset_growth > tolerance and abs(excess - reset_growth) <= tolerance


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
                if (
                    excess > 0
                    and _matches_false_zero_reset(
                        excess,
                        zero_anchor["state"],
                        current["state"],
                    )
                ):
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
        if (
            previous["state"] > 0
            and current["state"] > 0
            and excess > 0
            and _matches_false_zero_reset(
                excess,
                previous["state"],
                current["state"],
            )
        ):
            adjustments.append(
                PowerStatisticsAdjustment(
                    datetime.fromtimestamp(current["start"], timezone.utc),
                    -excess,
                )
            )

        previous = current

    return adjustments


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

    for entity_id, adjustments in adjustments_by_entity.items():
        unit = pending[entity_id][1]
        for adjustment in adjustments:
            recorder.async_adjust_statistics(
                entity_id,
                adjustment.start,
                adjustment.amount,
                unit,
            )

    if any(adjustments_by_entity.values()):
        await recorder.async_block_till_done()
        hourly, short_term = await recorder.async_add_executor_job(load_rows)

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

        adjustments = adjustments_by_entity[entity_id]
        completed[entity_id] = {
            "completed_at": now,
            "period": period,
            "adjustments": len(adjustments),
            "total_adjustment": round(
                sum(adjustment.amount for adjustment in adjustments),
                6,
            ),
            "unit": unit,
        }
        if adjustments:
            _LOGGER.warning(
                "Repaired %s historical false zero reset(s) for %s: %s %s",
                len(adjustments),
                entity_id,
                completed[entity_id]["total_adjustment"],
                unit,
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
