"""Offline checks for home-to-gateway scene selection."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

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
