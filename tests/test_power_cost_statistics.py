import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

import pytest
from homeassistant.util import dt

from custom_components.xiaomi_miot.core.templates import template


POWER_COMMAND = {
    "type": "stat_day_v3",
    "key": "11.1",
    "day": 32,
    "limit": 31,
    "attribute": None,
    "template": "micloud_statistics_power_cost",
}


def record(when, value):
    return {
        "time": int(when.timestamp()),
        "value": json.dumps([value]),
    }


def render_power_cost(hass, result):
    return template("micloud_statistics_power_cost", hass).async_render({
        "result": result,
    })


def power_device(make_device, load_miot_spec, response):
    device = make_device(
        load_miot_spec("cnhdm.airrtc.wkq01.json"),
        customizes={},
    )
    device.cloud = SimpleNamespace(
        async_request_api=AsyncMock(return_value=response),
    )
    device.dispatch = Mock()
    return device


def test_template_distinguishes_missing_from_zero(hass, freezer):
    now = datetime(2026, 7, 27, 12, tzinfo=ZoneInfo("UTC"))
    freezer.move_to(now)

    assert render_power_cost(hass, []) == {
        "power_cost_today": None,
        "power_cost_month": None,
    }
    assert render_power_cost(hass, [record(now, 0)]) == {
        "power_cost_today": 0,
        "power_cost_month": 0,
    }


def test_template_keeps_month_when_today_is_missing(hass, freezer):
    now = datetime(2026, 7, 27, 12, tzinfo=ZoneInfo("UTC"))
    freezer.move_to(now)

    assert render_power_cost(
        hass,
        [record(datetime(2026, 7, 26, 12, tzinfo=ZoneInfo("UTC")), 3.2)],
    ) == {
        "power_cost_today": None,
        "power_cost_month": 3.2,
    }


@pytest.mark.parametrize(
    "value",
    [
        None,
        "[]",
        "[null]",
        '["not-a-number"]',
        "[true]",
        "[-1]",
    ],
)
def test_template_ignores_invalid_records(hass, freezer, value):
    now = datetime(2026, 7, 27, 12, tzinfo=ZoneInfo("UTC"))
    freezer.move_to(now)
    result = [{"time": int(now.timestamp())}]
    if value is not None:
        result[0]["value"] = value

    assert render_power_cost(hass, result) == {
        "power_cost_today": None,
        "power_cost_month": None,
    }


@pytest.mark.parametrize(
    ("new_value", "accepted"),
    [
        (0, False),
        (16.8, False),
        (17.5, True),
        (17.8, True),
    ],
)
def test_daily_value_is_monotonic_within_local_day(
    make_device,
    load_miot_spec,
    new_value,
    accepted,
):
    device = power_device(make_device, load_miot_spec, None)
    device.props["power_cost_today"] = 17.5
    device.data["_power_cost_periods"] = {
        "power_cost_today": "2026-07-27",
    }
    now = datetime(2026, 7, 27, 23, tzinfo=ZoneInfo("Asia/Shanghai"))

    result = device._filter_power_cost_statistics(
        {"power_cost_today": new_value},
        now,
    )

    assert ("power_cost_today" in result) is accepted


@pytest.mark.parametrize("new_value", [0, 0.15, 1.0])
def test_daily_value_can_reset_across_local_day(
    make_device,
    load_miot_spec,
    new_value,
):
    device = power_device(make_device, load_miot_spec, None)
    device.props["power_cost_today"] = 22.4
    device.data["_power_cost_periods"] = {
        "power_cost_today": "2026-07-26",
    }
    now = datetime(2026, 7, 27, 0, 5, tzinfo=ZoneInfo("Asia/Shanghai"))

    assert device._filter_power_cost_statistics(
        {"power_cost_today": new_value},
        now,
    )["power_cost_today"] == new_value


@pytest.mark.parametrize(
    ("new_value", "accepted"),
    [
        (0, False),
        (319, False),
        (320, True),
        (321, True),
    ],
)
def test_month_value_is_monotonic_within_local_month(
    make_device,
    load_miot_spec,
    new_value,
    accepted,
):
    device = power_device(make_device, load_miot_spec, None)
    device.props["power_cost_month"] = 320
    device.data["_power_cost_periods"] = {
        "power_cost_month": "2026-07",
    }
    now = datetime(2026, 7, 27, tzinfo=ZoneInfo("Asia/Shanghai"))

    result = device._filter_power_cost_statistics(
        {"power_cost_month": new_value},
        now,
    )

    assert ("power_cost_month" in result) is accepted


@pytest.mark.parametrize("new_value", [0, 1.2])
def test_month_value_can_reset_across_local_month(
    make_device,
    load_miot_spec,
    new_value,
):
    device = power_device(make_device, load_miot_spec, None)
    device.props["power_cost_month"] = 450
    device.data["_power_cost_periods"] = {
        "power_cost_month": "2026-07",
    }
    now = datetime(2026, 8, 1, tzinfo=ZoneInfo("Asia/Shanghai"))

    assert device._filter_power_cost_statistics(
        {"power_cost_month": new_value},
        now,
    )["power_cost_month"] == new_value


def test_power_cost_suffixes_are_filtered_independently(
    make_device,
    load_miot_spec,
):
    device = power_device(make_device, load_miot_spec, None)
    device.props.update({
        "power_cost_today": 17.5,
        "power_cost_today_2": 8.0,
    })
    device.data["_power_cost_periods"] = {
        "power_cost_today": "2026-07-27",
        "power_cost_today_2": "2026-07-27",
    }
    attrs = {
        "power_cost_today": 16.8,
        "power_cost_today_2": 8.2,
        "other_attribute": "unchanged",
    }

    result = device._filter_power_cost_statistics(
        attrs,
        datetime(2026, 7, 27, tzinfo=ZoneInfo("UTC")),
    )

    assert result == {
        "power_cost_today_2": 8.2,
        "other_attribute": "unchanged",
    }
    assert attrs["power_cost_today"] == 16.8


def test_daily_period_remains_local_during_dst_fallback(
    make_device,
    load_miot_spec,
):
    device = power_device(make_device, load_miot_spec, None)
    device.props["power_cost_today"] = 17.5
    device.data["_power_cost_periods"] = {
        "power_cost_today": "2026-11-01",
    }
    now = datetime(
        2026,
        11,
        1,
        1,
        30,
        tzinfo=ZoneInfo("America/New_York"),
        fold=1,
    )

    assert device._filter_power_cost_statistics(
        {"power_cost_today": 16.8},
        now,
    ) == {}


@pytest.mark.parametrize(
    "value",
    [None, "", "not-a-number", True, float("nan"), float("inf"), -1],
)
def test_invalid_power_cost_values_are_removed(
    make_device,
    load_miot_spec,
    value,
):
    device = power_device(make_device, load_miot_spec, None)

    result = device._filter_power_cost_statistics(
        {
            "power_cost_today": value,
            "other_attribute": 1,
        },
        datetime(2026, 7, 27, tzinfo=ZoneInfo("UTC")),
    )

    assert result == {"other_attribute": 1}


@pytest.mark.parametrize(
    "response",
    [
        None,
        {},
        {"code": -1, "result": []},
        {"code": 0, "result": []},
        {"code": 0},
    ],
)
async def test_missing_cloud_data_preserves_previous_values(
    hass,
    make_device,
    load_miot_spec,
    response,
):
    device = power_device(make_device, load_miot_spec, response)
    device.props.update({
        "power_cost_today": 17.5,
        "power_cost_month": 320,
    })

    result = await device.update_cloud_statistics([POWER_COMMAND])

    assert result == {}
    assert device.props["power_cost_today"] == 17.5
    assert device.props["power_cost_month"] == 320
    device.dispatch.assert_not_called()


async def test_incomplete_month_does_not_replace_newer_values(
    make_device,
    load_miot_spec,
    freezer,
):
    now = datetime(2026, 7, 27, 12, tzinfo=ZoneInfo("UTC"))
    freezer.move_to(now)
    previous_day = now.replace(day=26)
    response = {
        "code": 0,
        "result": [record(previous_day, 3.2)],
    }
    device = power_device(make_device, load_miot_spec, response)
    device.props.update({
        "power_cost_today": 17.5,
        "power_cost_month": 320,
    })
    device.data["_power_cost_periods"] = {
        "power_cost_today": now.strftime("%Y-%m-%d"),
        "power_cost_month": now.strftime("%Y-%m"),
    }

    result = await device.update_cloud_statistics([POWER_COMMAND])

    assert result == {}
    assert device.props["power_cost_today"] == 17.5
    assert device.props["power_cost_month"] == 320


@pytest.mark.parametrize("value", [None, "", "not-json"])
async def test_invalid_json_does_not_break_cloud_update(
    make_device,
    load_miot_spec,
    value,
):
    now = dt.now()
    response = {
        "code": 0,
        "result": [{
            "time": int(now.timestamp()),
            "value": value,
        }],
    }
    device = power_device(make_device, load_miot_spec, response)

    assert await device.update_cloud_statistics([POWER_COMMAND]) == {}
    device.dispatch.assert_not_called()


async def test_multiple_power_keys_keep_their_suffix_when_one_is_missing(
    make_device,
    load_miot_spec,
):
    now = dt.now()
    device = power_device(make_device, load_miot_spec, None)
    device.cloud.async_request_api.side_effect = [
        None,
        {
            "code": 0,
            "result": [record(now, 8.2)],
        },
    ]

    result = await device.update_cloud_statistics([
        POWER_COMMAND,
        {**POWER_COMMAND, "key": "12.1"},
    ])

    assert result == {
        "power_cost_today_2": 8.2,
        "power_cost_month_2": 8.2,
    }
    assert "power_cost_today" not in device.props
    assert device.props["power_cost_today_2"] == 8.2
