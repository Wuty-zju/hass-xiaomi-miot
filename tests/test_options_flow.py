"""Tests for Xiaomi Miot options flow."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.xiaomi_miot.config_flow import OptionsFlowHandler


def _fake_show_form(*args, **kwargs):
    return {"type": "form", **kwargs}


async def test_options_cloud_schema_has_no_micoapi_verify():
    flow = OptionsFlowHandler.__new__(OptionsFlowHandler)
    entry = SimpleNamespace(
        data={
            "username": "u",
            "password": "p",
            "server_country": "cn",
        },
        options={},
    )
    flow.hass = SimpleNamespace(
        config_entries=SimpleNamespace(
            async_get_known_entry=lambda entry_id: entry,
        ),
    )
    flow.handler = "eid"
    flow.context = {}
    with patch.object(
        OptionsFlowHandler,
        "async_show_form",
        MagicMock(side_effect=_fake_show_form),
    ):
        result = await flow.async_step_cloud()

    schema_keys = {key.schema for key in result["data_schema"].schema}
    assert "micoapi_verify" not in schema_keys
    assert "scene_gateway_mode" in schema_keys
    assert result["data_schema"]({"username": "u", "password": "p",
                                  "server_country": "cn", "conn_mode": "auto"})[
        "scene_gateway_mode"] == "off"


async def test_options_open_original_cloud_form_and_mode_change_skips_login():
    flow = OptionsFlowHandler.__new__(OptionsFlowHandler)
    entry = SimpleNamespace(
        data={"username": "u", "password": "p", "server_country": "cn"},
        options={},
    )
    flow.hass = SimpleNamespace(config_entries=SimpleNamespace(
        async_get_known_entry=lambda entry_id: entry,
    ))
    flow.handler = "eid"
    flow.context = {}
    with patch.object(OptionsFlowHandler, "async_show_form",
                      MagicMock(side_effect=_fake_show_form)), patch.object(
        OptionsFlowHandler, "_async_set_gateway_mode",
        AsyncMock(return_value={"type": "create_entry"}),
    ) as set_mode, patch.object(
        OptionsFlowHandler, "check_xiaomi_account", AsyncMock(),
    ) as check_account:
        form = await flow.async_step_init()
        result = await flow.async_step_cloud({
            "username": "u", "password": "p", "server_country": "cn",
            "renew_devices": False, "trans_options": False,
            "disable_message": False, "disable_scene_history": False,
            "scene_gateway_mode": "independent",
        })

    assert form["step_id"] == "cloud"
    assert result["type"] == "create_entry"
    set_mode.assert_awaited_once_with("independent")
    check_account.assert_not_awaited()


def test_options_step_micoapi_is_removed():
    assert not hasattr(OptionsFlowHandler, "async_step_micoapi")
