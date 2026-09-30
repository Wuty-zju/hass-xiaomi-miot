"""The original Miot cloud form also configures the optional gateway."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from homeassistant.helpers.selector import SelectSelector

from custom_components.xiaomi_miot import config_flow
from custom_components.xiaomi_miot.core.const import CONF_SCENE_GATEWAY_MODE
from custom_components.xiaomi_miot.core.gateway_auth import GatewayAuthorizationError


def _flow():
    flow = SimpleNamespace(
        hass=SimpleNamespace(
            data={},
            config_entries=SimpleNamespace(async_entries=lambda domain: []),
        ),
        config_entry=SimpleNamespace(entry_id='miot', options={}),
        saved_config={'user_id': '1000', 'server_country': 'cn'},
        _gateway_account_context=lambda: ('1000', 'cn'),
        async_step_cloud=AsyncMock(return_value={'step_id': 'cloud', 'errors': {
            'base': 'gateway_reuse_not_configured',
        }}),
        async_create_entry=lambda **kwargs: kwargs,
        async_step_gateway_oauth=AsyncMock(return_value={'step_id': 'gateway_oauth'}),
        async_step_gateway_auth_choice=AsyncMock(return_value={'step_id': 'gateway_auth_choice'}),
    )
    return flow


async def test_gateway_is_off_by_default_and_off_needs_no_other_integration():
    flow = _flow()
    saved = await config_flow.OptionsFlowHandler._async_set_gateway_mode(
        flow, 'off',
    )
    assert saved['data'][CONF_SCENE_GATEWAY_MODE] == 'off'


async def test_reuse_requires_official_integration_and_independent_prompts_auth(monkeypatch):
    flow = _flow()
    result = await config_flow.OptionsFlowHandler._async_set_gateway_mode(flow, 'reuse')
    assert result['errors']['base'] == 'gateway_reuse_not_configured'

    monkeypatch.setattr(config_flow, 'gateway_store', lambda *args: SimpleNamespace(
        async_load=AsyncMock(return_value={}),
    ))
    result = await config_flow.OptionsFlowHandler._async_set_gateway_mode(flow, 'independent')
    assert result['step_id'] == 'gateway_oauth'
    flow.async_step_gateway_oauth.assert_awaited_once()


async def test_oauth_webhook_starts_one_exchange_immediately():
    flow = SimpleNamespace(
        _gateway_state='expected', _gateway_auth_task=None,
        _async_exchange_gateway_code=AsyncMock(),
    )
    flow._start_gateway_exchange = lambda code: config_flow.OptionsFlowHandler._start_gateway_exchange(flow, code)
    flow._gateway_exchange_done = config_flow.OptionsFlowHandler._gateway_exchange_done
    request = SimpleNamespace(query={'state': 'expected', 'code': 'one-time'})
    callback = config_flow.OptionsFlowHandler._gateway_oauth_webhook
    first = await callback(flow, None, 'hook', request)
    second = await callback(flow, None, 'hook', request)
    await flow._gateway_auth_task

    assert first.status == second.status == 200
    flow._async_exchange_gateway_code.assert_awaited_once_with('one-time')


async def test_oauth_rejection_starts_new_authorization_instead_of_pending():
    async def rejected():
        raise GatewayAuthorizationError('OAuth token request: Xiaomi code -6')

    flow = _flow()
    flow._gateway_webhook_id = 'hook'
    flow._gateway_auth_task = asyncio.create_task(rejected())
    flow._clear_gateway_webhook = lambda: setattr(flow, '_gateway_webhook_id', None)
    flow.async_step_gateway_oauth = AsyncMock(return_value={'step_id': 'gateway_oauth'})
    result = await config_flow.OptionsFlowHandler.async_step_gateway_oauth(flow, {})

    assert result['step_id'] == 'gateway_oauth'
    flow.async_step_gateway_oauth.assert_awaited_once_with(
        error='gateway_token_exchange_failed',
    )


@pytest.mark.parametrize('remove_flow', [False, True])
async def test_authorization_timeout_or_flow_removal_cancels_exchange(hass, remove_flow):
    flow = config_flow.OptionsFlowHandler(None)
    flow.hass = hass
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def exchange(code):
        started.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    flow._async_exchange_gateway_code = exchange
    flow._gateway_state = 'expected'
    flow._start_gateway_exchange('one-time')
    await started.wait()
    task = flow._gateway_auth_task
    if remove_flow:
        flow.async_remove()
    else:
        flow._expire_gateway_authorization()
    await asyncio.gather(task, return_exceptions=True)
    assert cancelled.is_set()
    assert task.cancelled()
    flow._start_gateway_exchange('late-code')
    assert flow._gateway_auth_task is task
    result = await flow._gateway_oauth_webhook(hass, 'old-hook', SimpleNamespace(
        query={'state': 'expected', 'code': 'late-code'},
    ))
    assert result.status == 400


async def test_completed_authorization_error_is_collected_and_still_awaitable():
    async def fail():
        raise GatewayAuthorizationError('rejected')

    task = asyncio.create_task(fail())
    await asyncio.sleep(0)
    config_flow.OptionsFlowHandler._gateway_exchange_done(task)
    with pytest.raises(GatewayAuthorizationError):
        await task


async def test_options_flow_removal_unregisters_webhook_and_cancels_timer(hass, monkeypatch):
    entry = MockConfigEntry(domain='xiaomi_miot', data={'user_id': '1000', 'server_country': 'cn'})
    entry.add_to_hass(hass)
    flow = config_flow.OptionsFlowHandler(entry)
    flow.hass = hass
    flow.handler = entry.entry_id
    monkeypatch.setattr(config_flow, 'async_get_instance_id', AsyncMock(return_value='instance'))
    await flow.async_step_gateway_oauth()
    webhook_id = flow._gateway_webhook_id
    timer = flow._gateway_webhook_timeout
    assert webhook_id in hass.data['webhook']
    flow.async_remove()
    assert webhook_id not in hass.data['webhook']
    assert timer.cancelled()


async def test_authorization_selector_has_stable_translatable_values(hass):
    flow = config_flow.OptionsFlowHandler(None)
    flow.hass = hass
    result = await flow.async_step_gateway_auth_choice()
    selector = next(iter(result['data_schema'].schema.values()))
    assert isinstance(selector, SelectSelector)
    assert selector.config['translation_key'] == 'gateway_auth_action'
    assert selector('existing') == 'existing'
    assert selector('renew') == 'renew'
