"""Offline checks for home-to-gateway scene selection."""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import threading
from dataclasses import replace

import pytest

from custom_components.xiaomi_miot.core.gateway_discovery import (
    GatewayAddress,
    home_group_id,
)
from custom_components.xiaomi_miot.core.gateway_manager import GatewayManager
from custom_components.xiaomi_miot.core.local_gateway import (
    GatewayResultUnknown,
    GatewayUnavailable,
)


SCENE = {'owner_uid': '1000', 'home_id': '123', 'scene_id': '42'}


def _manager(hass, groups=None, result=None):
    address = GatewayAddress('789', home_group_id('1000', '123'),
                             '192.0.2.10', 8883)
    gateway = SimpleNamespace(
        session=1,
        connect=AsyncMock(),
        action_groups=AsyncMock(return_value=groups if groups is not None else ['42']),
        run_action_group=AsyncMock(return_value=result or {'code': 1}),
    )
    manager = GatewayManager.__new__(GatewayManager)
    manager.hass = hass
    manager.cloud = SimpleNamespace(user_id='1000', default_server='cn')
    manager._closed = False
    manager._discovery = SimpleNamespace(
        for_group=lambda group_id: address if group_id == address.group_id else None,
        wait_for_group=AsyncMock(
            side_effect=lambda group_id: address if group_id == address.group_id else None,
        ),
    )
    manager._ensure_certificate = AsyncMock()
    manager._client_for = AsyncMock(return_value=gateway)
    return manager, gateway


async def test_gateway_manager_uses_exact_home_and_scene(hass):
    manager, gateway = _manager(hass)
    snapshot = await manager.qualify(SCENE)
    assert await snapshot.run(SCENE) is True
    gateway.run_action_group.assert_awaited_once_with('42', session=1)

    with pytest.raises(GatewayUnavailable):
        await snapshot.run({**SCENE, 'home_id': 'other'})
    assert gateway.run_action_group.await_count == 1


async def test_gateway_manager_missing_group_is_safe_to_fallback(hass):
    manager, gateway = _manager(hass, groups=['other'])
    with pytest.raises(GatewayUnavailable):
        snapshot = await manager.qualify(SCENE)
        await snapshot.run(SCENE)
    gateway.run_action_group.assert_not_awaited()


async def test_gateway_manager_rejected_execution_is_not_retried(hass):
    manager, gateway = _manager(hass, result={'code': -1})
    with pytest.raises(GatewayResultUnknown):
        snapshot = await manager.qualify(SCENE)
        await snapshot.run(SCENE)
    assert gateway.run_action_group.await_count == 1


async def test_snapshot_activation_has_no_list_or_setup_io(hass):
    manager, gateway = _manager(hass)
    snapshot = await manager.qualify(SCENE)
    assert snapshot.available(SCENE)
    manager._ensure_certificate.reset_mock()
    manager._client_for.reset_mock()
    manager._discovery.wait_for_group.reset_mock()
    gateway.connect.reset_mock()
    gateway.action_groups.reset_mock()
    assert await snapshot.run(SCENE)
    manager._ensure_certificate.assert_not_awaited()
    manager._client_for.assert_not_awaited()
    manager._discovery.wait_for_group.assert_not_awaited()
    gateway.connect.assert_not_awaited()
    gateway.action_groups.assert_not_awaited()
    gateway.run_action_group.assert_awaited_once_with('42', session=1)


@pytest.mark.parametrize('change', ['session', 'disconnect', 'address', 'account',
                                   'region', 'closed', 'expired', 'owner', 'home', 'scene'])
async def test_snapshot_revokes_without_sending_or_querying(hass, change):
    manager, gateway = _manager(hass)
    snapshot = await manager.qualify(SCENE)
    scene = dict(SCENE)
    if change == 'session':
        gateway.session += 1
    elif change == 'disconnect':
        gateway.session = None
    elif change == 'address':
        manager._discovery.for_group = lambda group_id: None
    elif change == 'account':
        manager.cloud.user_id = '2000'
    elif change == 'region':
        manager.cloud.default_server = 'de'
    elif change == 'closed':
        manager._closed = True
    elif change == 'expired':
        snapshot = replace(snapshot, expires=hass.loop.time() - 1)
    else:
        scene[{'owner': 'owner_uid', 'home': 'home_id', 'scene': 'scene_id'}[change]] = 'other'
    assert not snapshot.available(scene)
    with pytest.raises(GatewayUnavailable):
        await snapshot.run(scene)
    gateway.run_action_group.assert_not_awaited()
    assert gateway.action_groups.await_count == 1


async def test_reconnection_during_list_read_cannot_grant_qualification(hass):
    manager, gateway = _manager(hass)

    async def groups(timeout):
        gateway.session += 1
        return ['42']

    gateway.action_groups.side_effect = groups
    with pytest.raises(GatewayUnavailable):
        await manager.qualify(SCENE)
    gateway.run_action_group.assert_not_awaited()


@pytest.mark.parametrize('code', [True, False, '1', None])
async def test_snapshot_does_not_accept_untyped_business_success(hass, code):
    manager, gateway = _manager(hass, result={'code': code})
    snapshot = await manager.qualify(SCENE)
    with pytest.raises(GatewayResultUnknown):
        await snapshot.run(SCENE)
    assert gateway.run_action_group.await_count == 1


async def test_qualification_is_bounded_and_cancellation_is_not_swallowed(hass):
    manager, gateway = _manager(hass)
    entered = asyncio.Event()

    async def blocked():
        entered.set()
        await asyncio.Event().wait()

    manager._ensure_certificate.side_effect = blocked
    with pytest.raises(GatewayUnavailable):
        await manager.qualify(SCENE, timeout=0.001)
    entered.clear()
    task = asyncio.create_task(manager.qualify(SCENE))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    gateway.run_action_group.assert_not_awaited()


@pytest.mark.parametrize('home', [
    {**SCENE, 'owner_uid': True}, {**SCENE, 'home_id': ''}, {**SCENE, 'home_id': {}},
])
async def test_invalid_scope_is_rejected_before_network_io(hass, home):
    manager, gateway = _manager(hass)
    with pytest.raises(GatewayUnavailable):
        await manager.qualify(home)
    manager._ensure_certificate.assert_not_awaited()
    manager._discovery.wait_for_group.assert_not_awaited()


@pytest.mark.parametrize('days', [10, 1])
async def test_valid_certificate_survives_expired_oauth_and_offline_renewal(hass, days):
    manager = GatewayManager(hass, 'test-entry', SimpleNamespace(
        user_id='1000', default_server='cn',
    ))
    manager._credentials = {
        'uid': '1000', 'region': 'cn', 'expires_at': 0,
        'certificate': 'test-certificate', 'private_key': 'test-key',
        'virtual_did': 'virtual-id', 'redirect_uri': 'http://example.test',
        'oauth_device_id': 'test-device', 'refresh_token': 'test-token',
    }
    expires = datetime.now(timezone.utc) + timedelta(days=days)
    with patch('custom_components.xiaomi_miot.core.gateway_manager.validate_gateway_certificate', return_value=expires), patch(
        'custom_components.xiaomi_miot.core.gateway_manager.exchange_token',
        new=AsyncMock(side_effect=TimeoutError),
    ) as refresh:
        await manager._ensure_certificate()
        assert refresh.await_count == int(days <= 3)
    assert manager._credentials['certificate'] == 'test-certificate'


async def test_concurrent_gateway_creation_shares_one_client(hass, tmp_path):
    manager = GatewayManager(hass, 'test-entry', SimpleNamespace())
    manager._directory = SimpleNamespace(name=str(tmp_path))
    manager._credentials = {
        'certificate': 'test-cert', 'private_key': 'test-key',
        'virtual_did': 'virtual-id',
    }
    address = GatewayAddress('789', 'home-group', '192.0.2.10', 8883)
    client = SimpleNamespace(close=AsyncMock())
    loop_thread = threading.get_ident()
    context = object()

    def load_context(*args):
        assert threading.get_ident() != loop_thread
        return context

    with patch('custom_components.xiaomi_miot.core.gateway_manager.LocalGatewayClient', return_value=client) as factory, patch(
        'custom_components.xiaomi_miot.core.gateway_manager.gateway_ssl_context',
        side_effect=load_context,
    ) as load, patch.object(
        hass, 'async_add_executor_job',
        side_effect=lambda target, *args: asyncio.to_thread(target, *args),
    ):
        clients = await asyncio.gather(manager._client_for(address), manager._client_for(address))
        assert clients == [client, client]
        factory.assert_called_once_with('192.0.2.10', 8883, 'virtual-id', context)
        load.assert_called_once()
    manager._directory = None
    with pytest.raises(GatewayUnavailable):
        await manager._client_for(address)
