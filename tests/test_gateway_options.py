"""The original Miot cloud form also configures the optional gateway."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from custom_components.xiaomi_miot import config_flow
from custom_components.xiaomi_miot.core.const import CONF_SCENE_GATEWAY_MODE


def _flow():
    flow = SimpleNamespace(
        hass=SimpleNamespace(
            data={},
            config_entries=SimpleNamespace(async_entries=lambda domain: []),
        ),
        config_entry=SimpleNamespace(entry_id='miot', options={}),
        saved_config={'user_id': '1000', 'server_country': 'cn'},
        async_step_cloud=AsyncMock(return_value={'step_id': 'cloud', 'errors': {
            'base': 'gateway_reuse_not_configured',
        }}),
        async_create_entry=lambda **kwargs: kwargs,
        async_step_gateway_oauth=AsyncMock(return_value={'step_id': 'gateway_oauth'}),
        async_step_gateway_auth_choice=AsyncMock(return_value={'step_id': 'gateway_auth_choice'}),
    )
    return flow


def test_gateway_is_off_by_default_and_off_needs_no_other_integration():
    async def run():
        flow = _flow()
        saved = await config_flow.OptionsFlowHandler._async_set_gateway_mode(
            flow, 'off',
        )
        assert saved['data'][CONF_SCENE_GATEWAY_MODE] == 'off'

    asyncio.run(run())


def test_reuse_requires_official_integration_and_independent_prompts_auth(monkeypatch):
    async def run():
        flow = _flow()
        result = await config_flow.OptionsFlowHandler._async_set_gateway_mode(flow, 'reuse')
        assert result['errors']['base'] == 'gateway_reuse_not_configured'

        monkeypatch.setattr(config_flow, 'gateway_store', lambda *args: SimpleNamespace(
            async_load=AsyncMock(return_value={}),
        ))
        result = await config_flow.OptionsFlowHandler._async_set_gateway_mode(flow, 'independent')
        assert result['step_id'] == 'gateway_oauth'
        flow.async_step_gateway_oauth.assert_awaited_once()

    asyncio.run(run())
