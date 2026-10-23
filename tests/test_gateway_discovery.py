"""Offline validation of central-gateway mDNS advertisements."""

import base64
import asyncio
from types import SimpleNamespace
from unittest.mock import Mock, patch, AsyncMock

from zeroconf import ServiceStateChange

from custom_components.xiaomi_miot.core.gateway_discovery import (
    GatewayAddress,
    GatewayDiscovery,
    SERVICE_TYPE,
    home_group_id,
    parse_gateway_service,
)


def _advertisement(role=1, mqtt=True):
    profile = bytearray(23)
    profile[1:9] = (123456).to_bytes(8, 'big')
    profile[9:17] = bytes.fromhex('0807060504030201')
    profile[20] = role << 4
    profile[22] = 2 if mqtt else 0
    return SimpleNamespace(
        properties={b'profile': base64.b64encode(profile)},
        parsed_addresses=lambda version: ['192.0.2.10'],
        port=8883,
    )


def test_parse_gateway_service():
    assert parse_gateway_service(_advertisement()) == GatewayAddress(
        did='123456', group_id='0102030405060708',
        host='192.0.2.10', port=8883,
    )


def test_home_group_id_is_deterministic():
    assert home_group_id('1000', '123') == '2a47780246efeac8'


def test_reject_non_central_or_non_mqtt_service():
    assert parse_gateway_service(_advertisement(role=2)) is None
    assert parse_gateway_service(_advertisement(mqtt=False)) is None


def test_reject_invalid_profile():
    advertisement = _advertisement()
    advertisement.properties[b'profile'] = b'not base64%'
    assert parse_gateway_service(advertisement) is None


async def test_close_cancels_pending_discovery_and_prevents_late_addition():
    discovery = GatewayDiscovery(None)
    started = asyncio.Event()

    async def resolve(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    advertisement = _advertisement()
    advertisement.async_request = resolve
    with patch('custom_components.xiaomi_miot.core.gateway_discovery.AsyncServiceInfo', return_value=advertisement):
        discovery._service_changed(None, SERVICE_TYPE, 'test-service', ServiceStateChange.Added)
        await started.wait()
        await discovery.close()
        discovery._service_changed(None, SERVICE_TYPE, 'test-service', ServiceStateChange.Added)
    assert not discovery._tasks
    assert not discovery.gateways
    assert await discovery.wait_for_group('missing') is None


async def test_removed_service_cancels_its_pending_refresh():
    discovery = GatewayDiscovery(None)
    started = asyncio.Event()

    async def resolve(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    advertisement = _advertisement()
    advertisement.async_request = resolve
    with patch('custom_components.xiaomi_miot.core.gateway_discovery.AsyncServiceInfo', return_value=advertisement):
        discovery._service_changed(None, SERVICE_TYPE, 'test-service', ServiceStateChange.Added)
        await started.wait()
        task = discovery._tasks['test-service']
        discovery._service_changed(None, SERVICE_TYPE, 'test-service', ServiceStateChange.Removed)
        await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled()
    assert not discovery.gateways
    await discovery.close()


async def test_group_watch_notifies_changed_endpoint_and_removal_without_waiting():
    discovery = GatewayDiscovery(None)
    notify, unrelated = Mock(), Mock()
    unsubscribe = discovery.watch('0102030405060708', notify)
    discovery.watch('other', unrelated)
    info = _advertisement()
    info.async_request = AsyncMock(return_value=True)
    with patch('custom_components.xiaomi_miot.core.gateway_discovery.AsyncServiceInfo', return_value=info):
        await discovery._refresh(SERVICE_TYPE, 'service')
        notify.assert_called_once()
        await discovery._refresh(SERVICE_TYPE, 'service')
        assert notify.call_count == 1  # Unchanged periodic advertisement.
        info.parsed_addresses = lambda version: ['192.0.2.11']
        await discovery._refresh(SERVICE_TYPE, 'service')
    assert notify.call_count == 2
    unrelated.assert_not_called()
    discovery._service_changed(None, SERVICE_TYPE, 'service', ServiceStateChange.Removed)
    assert notify.call_count == 3 and discovery.for_group('0102030405060708') is None
    unsubscribe()
    await discovery.close()
    assert notify.call_count == 3 and not discovery._watchers
