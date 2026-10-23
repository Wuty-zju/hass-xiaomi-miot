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
