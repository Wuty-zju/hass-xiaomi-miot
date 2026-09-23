"""No-device tests for manual scene connection-mode routing."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.xiaomi_miot.button import ManualSceneButton
from custom_components.xiaomi_miot.core.local_gateway import (
    GatewayResultUnknown,
    GatewayUnavailable,
)


SCENE = {
    'owner_uid': '1000',
    'scene_id': '42',
    'scene_name': 'Sleep',
    'home_id': '123',
    'home_name': 'Home',
}


def _button(mode, local_result=True):
    cloud = SimpleNamespace(
        unique_id='1000-cn-xiaomiio',
        async_run_manual_scene=AsyncMock(return_value=True),
    )
    manager = SimpleNamespace(run_scene=AsyncMock(return_value=local_result))
    entry = SimpleNamespace(
        get_config=lambda key: mode,
        local_gateway=manager,
    )
    return ManualSceneButton(cloud, SCENE, 'Sleep', entry), cloud, manager


@pytest.mark.parametrize('mode,local_calls,cloud_calls', [
    ('auto', 1, 0),
    ('local', 1, 0),
    ('cloud', 0, 1),
])
def test_scene_routing_success(mode, local_calls, cloud_calls):
    async def run():
        button, cloud, manager = _button(mode)
        await button.async_press()
        assert manager.run_scene.await_count == local_calls
        assert cloud.async_run_manual_scene.await_count == cloud_calls

    asyncio.run(run())


def test_auto_falls_back_only_before_local_dispatch():
    async def run():
        button, cloud, manager = _button('auto')
        manager.run_scene.side_effect = GatewayUnavailable('no gateway')
        await button.async_press()
        cloud.async_run_manual_scene.assert_awaited_once_with(SCENE)

    asyncio.run(run())


def test_local_mode_never_uses_cloud():
    async def run():
        button, cloud, manager = _button('local')
        manager.run_scene.side_effect = GatewayUnavailable('no gateway')
        with pytest.raises(HomeAssistantError, match='Local'):
            await button.async_press()
        cloud.async_run_manual_scene.assert_not_awaited()

    asyncio.run(run())


def test_unknown_local_result_never_retries_in_cloud():
    async def run():
        button, cloud, manager = _button('auto')
        manager.run_scene.side_effect = GatewayResultUnknown('timeout')
        with pytest.raises(HomeAssistantError, match='result unknown'):
            await button.async_press()
        cloud.async_run_manual_scene.assert_not_awaited()

    asyncio.run(run())
