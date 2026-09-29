"""Offline checks for home-to-gateway scene selection."""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import patch

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


def _manager(groups=None, result=None):
    address = GatewayAddress('789', home_group_id('1000', '123'),
                             '192.0.2.10', 8883)
    gateway = SimpleNamespace(
        connect=AsyncMock(),
        action_groups=AsyncMock(return_value=groups if groups is not None else ['42']),
        run_action_group=AsyncMock(return_value=result or {'code': 1}),
    )
    manager = GatewayManager.__new__(GatewayManager)
    manager._discovery = SimpleNamespace(
        wait_for_group=AsyncMock(
            side_effect=lambda group_id: address if group_id == address.group_id else None,
        ),
    )
    manager._ensure_certificate = AsyncMock()
    manager._client_for = AsyncMock(return_value=gateway)
    return manager, gateway


def test_gateway_manager_uses_exact_home_and_scene():
    async def run():
        manager, gateway = _manager()
        assert await manager.run_scene(SCENE) is True
        gateway.run_action_group.assert_awaited_once_with('42')

        with pytest.raises(GatewayUnavailable):
            await manager.run_scene({**SCENE, 'home_id': 'other'})
        assert gateway.run_action_group.await_count == 1

    asyncio.run(run())


def test_gateway_manager_missing_group_is_safe_to_fallback():
    async def run():
        manager, gateway = _manager(groups=['other'])
        with pytest.raises(GatewayUnavailable):
            await manager.run_scene(SCENE)
        gateway.run_action_group.assert_not_awaited()

    asyncio.run(run())


def test_gateway_manager_rejected_execution_is_not_retried():
    async def run():
        manager, gateway = _manager(result={'code': -1})
        with pytest.raises(GatewayResultUnknown):
            await manager.run_scene(SCENE)
        assert gateway.run_action_group.await_count == 1

    asyncio.run(run())


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
    with patch('custom_components.xiaomi_miot.core.gateway_manager.LocalGatewayClient', return_value=client) as factory:
        clients = await asyncio.gather(manager._client_for(address), manager._client_for(address))
        assert clients == [client, client]
        factory.assert_called_once()
    manager._directory = None
    with pytest.raises(GatewayUnavailable):
        await manager._client_for(address)
