"""Account routing uses background home snapshots, never activation-time lists."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.xiaomi_miot.button import ManualSceneButton
from custom_components.xiaomi_miot.core.gateway_scenes import GatewayScenes
from custom_components.xiaomi_miot.core.local_gateway import GatewayResultUnknown, GatewayUnavailable

SCENE = {'owner_uid': '1000', 'home_id': '123', 'scene_id': '42',
         'scene_name': 'Sleep', 'home_name': 'Home'}
KEY = ('123', '42')


@pytest.fixture
async def buttons(hass):
    managers = []

    async def make(mode='auto', source='independent', valid=True):
        cloud = SimpleNamespace(
            unique_id='1000-cn-xiaomiio', async_run_manual_scene=AsyncMock(return_value=True),
        )
        snapshot = SimpleNamespace(
            available=Mock(return_value=valid), run=AsyncMock(return_value=True),
            watch=Mock(return_value=Mock()),
        )
        backend = SimpleNamespace(qualify=AsyncMock(return_value=snapshot), close=AsyncMock())
        settings = {'conn_mode': mode, 'scene_gateway_mode': source}
        entry = SimpleNamespace(
            id=f'entry-{len(managers)}', hass=hass, cloud=cloud, local_gateway=backend,
            get_config=lambda key, default=None: settings.get(key, default),
        )
        manager = GatewayScenes(entry)
        manager._loaded = True
        manager._scenes = {KEY: dict(SCENE)}
        managers.append(manager)
        manager.notify()
        await asyncio.sleep(0)
        return ManualSceneButton(cloud, SCENE, 'Sleep', manager), cloud, backend, snapshot

    yield make
    for manager in managers:
        await manager.async_close()


@pytest.mark.parametrize('mode,local_calls,cloud_calls', [
    ('auto', 1, 0), ('local', 1, 0), ('cloud', 0, 1),
])
async def test_scene_routing_success(buttons, mode, local_calls, cloud_calls):
    button, cloud, backend, snapshot = await buttons(mode)
    reads = backend.qualify.await_count
    assert button.available
    assert button.icon == ('mdi:cloud-outline' if mode == 'cloud' else 'mdi:hub-outline')
    await button.async_press()
    assert snapshot.run.await_count == local_calls
    assert cloud.async_run_manual_scene.await_count == cloud_calls
    assert backend.qualify.await_count == reads
    if mode == 'cloud':
        backend.qualify.assert_not_awaited()


async def test_auto_falls_back_only_before_local_dispatch(buttons):
    button, cloud, backend, snapshot = await buttons()
    snapshot.run.side_effect = GatewayUnavailable('known not sent')
    await button.async_press()
    cloud.async_run_manual_scene.assert_awaited_once_with(SCENE)


async def test_local_mode_never_uses_cloud(buttons):
    button, cloud, backend, snapshot = await buttons('local', valid=False)
    assert not button.available and button.icon == 'mdi:cloud-off-outline'
    with pytest.raises(HomeAssistantError) as exc:
        await button.async_press()
    assert exc.value.translation_key == 'scene_unavailable'
    cloud.async_run_manual_scene.assert_not_awaited()
    snapshot.run.assert_not_awaited()


async def test_unknown_local_result_never_retries_in_cloud(buttons):
    button, cloud, backend, snapshot = await buttons()
    snapshot.run.side_effect = GatewayResultUnknown('timeout after dispatch')
    with pytest.raises(HomeAssistantError) as exc:
        await button.async_press()
    assert exc.value.translation_key == 'scene_result_unknown'
    cloud.async_run_manual_scene.assert_not_awaited()
    assert snapshot.run.await_count == 1


async def test_gateway_off_uses_cloud_even_for_local_device_mode(buttons):
    button, cloud, backend, snapshot = await buttons('local', source='off')
    await button.async_press()
    backend.qualify.assert_not_awaited()
    snapshot.run.assert_not_awaited()
    cloud.async_run_manual_scene.assert_awaited_once_with(SCENE)


async def test_connection_revocation_changes_route_and_wakes_one_reader(buttons):
    button, cloud, backend, snapshot = await buttons()
    assert button.icon == 'mdi:hub-outline'
    snapshot.available.return_value = False
    callback = snapshot.watch.call_args.args[0]
    for _ in range(10):
        callback()
    assert button.icon == 'mdi:cloud-outline'
    # The event is coalesced, not a list read from a property getter.
    assert backend.qualify.await_count == 1
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert backend.qualify.await_count <= 2


async def test_different_homes_progress_and_removed_home_is_cancelled(buttons):
    button, cloud, backend, snapshot = await buttons()
    manager = button.manager
    entered = asyncio.Event()

    async def qualify(home):
        if home['home_id'] == 'blocked':
            entered.set()
            await asyncio.Event().wait()
        return snapshot

    backend.qualify.side_effect = qualify
    manager._scenes[('blocked', '43')] = {**SCENE, 'home_id': 'blocked', 'scene_id': '43'}
    manager.notify()
    await entered.wait()
    assert manager.route(KEY) == 'local'
    blocked = manager._workers[('1000', 'blocked')][2]
    manager._scenes.pop(('blocked', '43'))
    manager.notify()
    await asyncio.gather(blocked, return_exceptions=True)
    assert blocked.cancelled()
    assert ('1000', 'blocked') not in manager._snapshots
    await manager.async_close()
    assert not manager._workers and not manager._retired and not manager._snapshots


async def test_failed_qualification_does_not_persist_old_eligibility(buttons):
    button, cloud, backend, snapshot = await buttons()
    backend.qualify.side_effect = GatewayUnavailable('read failed')
    snapshot.watch.call_args.args[0]()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert button.manager._snapshots == {}
    assert button.icon == 'mdi:cloud-outline'
    assert backend.qualify.await_count == 2


async def test_ha_stop_cancels_workers_and_closes_owned_backend(buttons):
    button, cloud, backend, snapshot = await buttons()
    manager = button.manager
    worker = next(iter(manager._workers.values()))[2]
    await manager.async_close(event=SimpleNamespace())
    assert worker.cancelled() and not manager._snapshots and not manager._workers
    backend.close.assert_awaited_once()
    snapshot.watch.return_value.assert_called_once_with()


async def test_explicit_local_rejection_is_not_unknown_or_replayed(buttons):
    from custom_components.xiaomi_miot.core.local_gateway import GatewayRejected
    button, cloud, backend, snapshot = await buttons()
    snapshot.run.side_effect = GatewayRejected('rejected')
    with pytest.raises(HomeAssistantError) as exc:
        await button.async_press()
    assert exc.value.translation_key == 'scene_failed'
    cloud.async_run_manual_scene.assert_not_awaited()
