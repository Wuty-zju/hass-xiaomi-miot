"""Synthetic borrowed transports: no import of Home, credentials or MQTT."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState

from custom_components.xiaomi_miot.core import xiaomi_home_gateway as bridge
from custom_components.xiaomi_miot.core.scene_gateway import GatewayResultUnknown, GatewayUnavailable, home_group_id

SCENE = {'owner_uid': '1000', 'home_id': '123', 'scene_id': '42'}
GROUP = home_group_id('1000', '123')


@pytest.fixture
def borrowed(hass):
    ready = asyncio.Event()
    ready.set()
    local = SimpleNamespace(
        mips_state=True, _reply_generation=7, _reply_ready_generation=7,
        _reply_ready=ready, sub_mips_state=Mock(), unsub_mips_state=Mock(),
        disconnect=Mock(), close=AsyncMock(),
    )
    client = SimpleNamespace(
        _mips_local={GROUP: local}, sub_manual_scene_state=Mock(), unsub_manual_scene_state=Mock(),
        _scene_routes={GROUP: SimpleNamespace(client=local, scene_ids=frozenset({'42'}))},
    )
    entry = SimpleNamespace(
        entry_id='official', state=ConfigEntryState.LOADED, async_on_state_change=Mock(return_value=Mock()),
        data={'uid': '1000', 'cloud_server': 'cn',
              'home_selected': {'123': {'uid': '1000', 'group_id': GROUP}}},
    )
    hass.data['xiaomi_home'] = {'miot_clients': {'official': client}}

    def request(topic, payload, received, **kwargs):
        assert kwargs['generation'] == 7
        received('{"result":{"code":1}}', kwargs['on_reply_ctx'])
        return True

    local._MipsLocalClient__request_external = Mock(side_effect=request)
    backend = bridge.XiaomiHomeGateway(hass, '1000', 'cn')
    with patch.object(hass.config_entries, 'async_entries', return_value=[entry]), patch.object(
        bridge, 'async_get_integration', AsyncMock(return_value=SimpleNamespace(
            version=bridge.VERIFIED_VERSION, file_path=None,
        )),
    ) as loader, patch.object(bridge, '_fingerprint', return_value=True) as fingerprint:
        yield backend, local, client, entry, loader, fingerprint


async def test_borrowing_uses_existing_memory_qualification_and_expected_generation(borrowed):
    backend, local, client, entry, loader, fingerprint = borrowed
    snapshot = await backend.qualify(SCENE)
    assert snapshot.available(SCENE)
    assert await snapshot.run(SCENE)
    rpc = local._MipsLocalClient__request_external
    assert rpc.call_count == 1 and rpc.call_args.args[0] == 'proxy/execMijiaActionGroup'
    assert rpc.call_args.kwargs['generation'] == 7
    fingerprint.assert_called_once()
    await backend.close()
    assert not snapshot.available(SCENE)
    local.close.assert_not_awaited()
    local.disconnect.assert_not_called()


@pytest.mark.parametrize('change', ['uid', 'region', 'home_owner', 'group', 'unloaded',
                                   'generation', 'ready_generation', 'client', 'route', 'groups'])
async def test_borrowed_snapshot_revokes_identity_session_and_runtime_changes(borrowed, change):
    backend, local, client, entry, loader, fingerprint = borrowed
    snapshot = await backend.qualify(SCENE)
    if change == 'uid': entry.data['uid'] = '2000'
    elif change == 'region': entry.data['cloud_server'] = 'de'
    elif change == 'home_owner': entry.data['home_selected']['123']['uid'] = '2000'
    elif change == 'group': entry.data['home_selected']['123']['group_id'] = 'wrong'
    elif change == 'unloaded': entry.state = ConfigEntryState.NOT_LOADED
    elif change == 'generation': local._reply_generation += 1
    elif change == 'ready_generation': local._reply_ready_generation = None
    elif change == 'client': client._mips_local.clear()
    elif change == 'route': client._scene_routes.clear()
    elif change == 'groups': client._scene_routes[GROUP].scene_ids = frozenset()
    assert not snapshot.available(SCENE)
    with pytest.raises(GatewayUnavailable):
        await snapshot.run(SCENE)
    local._MipsLocalClient__request_external.assert_not_called()


@pytest.mark.parametrize('version,fingerprint_ok', [('v0.5.0', True), ('v9.0.0', True),
                                                  (bridge.VERIFIED_VERSION, False)])
async def test_unknown_version_or_source_fingerprint_is_closed(borrowed, version, fingerprint_ok):
    backend, local, client, entry, loader, fingerprint = borrowed
    loader.return_value.version = version
    fingerprint.return_value = fingerprint_ok
    with pytest.raises(GatewayUnavailable):
        await backend.qualify(SCENE)
    assert not backend._compatible
    local._MipsLocalClient__request_external.assert_not_called()


async def test_subscriptions_belong_to_adapter_and_not_transport(borrowed):
    backend, local, client, entry, loader, fingerprint = borrowed
    snapshot = await backend.qualify(SCENE)
    notify = Mock()
    unsubscribe = snapshot.watch(notify)
    assert client.sub_manual_scene_state.call_args.args[0].startswith('xiaomi_miot-scene-')
    await local.sub_mips_state.call_args.args[1](GROUP, False)
    notify.assert_called_once()
    unsubscribe()
    client.unsub_manual_scene_state.assert_called_once()
    local.unsub_mips_state.assert_called_once()
    entry.async_on_state_change.return_value.assert_called_once()
    local.close.assert_not_awaited()


@pytest.mark.parametrize('reply', ['[]', '{"error":null}', '{"result":{"code":true}}', 'not-json'])
async def test_invalid_borrowed_reply_is_unknown_and_not_replayed(borrowed, reply):
    backend, local, client, entry, loader, fingerprint = borrowed
    snapshot = await backend.qualify(SCENE)

    def request(topic, payload, received, **kwargs):
        received(reply, kwargs['on_reply_ctx'])
        return True

    local._MipsLocalClient__request_external.side_effect = request
    with pytest.raises(GatewayResultUnknown):
        await snapshot.run(SCENE)
    assert local._MipsLocalClient__request_external.call_count == 1


async def test_bridge_runs_through_account_background_route_without_new_lists(borrowed):
    from custom_components.xiaomi_miot.core.gateway_scenes import GatewayScenes
    backend, local, client, entry, loader, fingerprint = borrowed
    qualified = asyncio.Event()
    qualify = backend.qualify

    async def read(home):
        snapshot = await qualify(home)
        qualified.set()
        return snapshot

    backend.qualify = AsyncMock(side_effect=read)
    cloud = SimpleNamespace(unique_id='demo-cn', async_run_manual_scene=AsyncMock())
    settings = {'conn_mode': 'auto', 'scene_gateway_mode': 'reuse'}
    owner = SimpleNamespace(id='miot', hass=backend.hass, cloud=cloud, local_gateway=backend,
                            get_config=lambda key, default=None: settings.get(key, default))
    manager = GatewayScenes(owner)
    manager._loaded = True
    manager._scenes = {('123', '42'): {**SCENE, 'scene_name': 'Sleep', 'home_name': 'Home'}}
    try:
        manager.notify()
        await qualified.wait()
        assert manager.route(('123', '42')) == 'local'
        await manager.async_run(('123', '42'))
        assert backend.qualify.await_count == 1
        cloud.async_run_manual_scene.assert_not_awaited()
        assert local._MipsLocalClient__request_external.call_count == 1
    finally:
        await manager.async_close()
    local.close.assert_not_awaited()


async def test_cancelled_bridge_reply_is_ignored_without_replay(borrowed):
    backend, local, client, entry, loader, fingerprint = borrowed
    snapshot = await backend.qualify(SCENE)
    sent = asyncio.Event()
    received = []

    def request(topic, payload, callback, **kwargs):
        received.append((callback, kwargs['on_reply_ctx']))
        sent.set()
        return True

    local._MipsLocalClient__request_external.side_effect = request
    task = asyncio.create_task(snapshot.run(SCENE))
    await sent.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert received[0][1].cancelled()
    received[0][0]('{"result":{"code":1}}', received[0][1])
    await asyncio.sleep(0)
    assert local._MipsLocalClient__request_external.call_count == 1


async def test_reuse_option_preserves_other_options_without_authentication():
    from custom_components.xiaomi_miot.config_flow import OptionsFlowHandler
    flow = SimpleNamespace(
        saved_config={'scene_gateway_mode': 'off'},
        config_entry=SimpleNamespace(options={'disable_message': True}),
        async_create_entry=lambda **kwargs: kwargs,
        check_xiaomi_account=AsyncMock(),
    )
    result = await OptionsFlowHandler.async_step_cloud(flow, {'scene_gateway_mode': 'reuse'})
    assert result['data'] == {'disable_message': True, 'scene_gateway_mode': 'reuse'}
    flow.check_xiaomi_account.assert_not_awaited()
