"""Offline checks for the independent central-gateway wire protocol."""

import asyncio
import json
import struct
from types import SimpleNamespace
from unittest.mock import Mock, patch
import ssl

import pytest
import paho.mqtt.client as mqtt

from custom_components.xiaomi_miot.core.local_gateway import (
    GatewayResultUnknown,
    GatewayUnavailable,
    LocalGatewayClient,
    decode_message,
    encode_message,
    gateway_ssl_context,
)


def test_gateway_message_round_trip():
    packet = encode_message(23, '{"id":"42"}', 'virtual-did/reply')

    assert decode_message(packet) == (23, '{"id":"42"}')
    assert packet.count(b'virtual-did/reply') == 1


@pytest.mark.parametrize('packet', [
    b'\x01',
    struct.pack('<IB', 100, 2) + b'{}',
    struct.pack('<IBI', 4, 0, 23),
])
def test_gateway_message_rejects_incomplete_reply(packet):
    with pytest.raises(ValueError):
        decode_message(packet)


def _fake_gateway(publish_rc=0):
    gateway = LocalGatewayClient.__new__(LocalGatewayClient)
    gateway._loop = asyncio.get_running_loop()
    gateway._reply_topic = 'virtual-did/reply'
    gateway._ready = asyncio.Event()
    gateway._ready.set()
    gateway._pending = {}
    gateway._next_id = 22
    gateway._closed = False
    gateway._started = False
    gateway._connected = True
    publications = []

    def publish(topic, packet, qos):
        publications.append((topic, packet, qos))
        if publish_rc == 0:
            message_id, _ = decode_message(packet)
            gateway._resolve(message_id, json.dumps({'result': {'code': 1}}))
        return SimpleNamespace(rc=publish_rc)

    gateway._client = SimpleNamespace(publish=publish)
    return gateway, publications


async def test_gateway_request_uses_local_topic_and_correlates_reply():
    gateway, publications = _fake_gateway()
    result = await gateway.run_action_group('42')

    assert result == {'code': 1}
    assert publications[0][0] == 'master/proxy/execMijiaActionGroup'
    assert publications[0][2] == 2
    assert b'"id":"42"' in publications[0][1]
    assert not gateway._pending


async def test_gateway_publish_failure_is_not_safe_to_retry():
    gateway, _ = _fake_gateway(publish_rc=4)
    with pytest.raises(GatewayResultUnknown):
        await gateway.run_action_group('42')
    assert not gateway._pending


async def test_real_paho_disconnected_publish_retains_the_command():
    gateway, _ = _fake_gateway()
    gateway._client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2, protocol=mqtt.MQTTv5,
    )
    with pytest.raises(GatewayResultUnknown):
        await gateway.run_action_group('42')
    assert len(gateway._client._out_messages) == 1
    assert not gateway._pending


async def test_gateway_timeout_has_unknown_outcome():
    gateway, _ = _fake_gateway()
    gateway._client.publish = lambda *args, **kwargs: SimpleNamespace(rc=0)
    with pytest.raises(GatewayResultUnknown):
        await gateway.request('proxy/execMijiaActionGroup', {'id': '42'},
                              timeout=0.001)
    assert not gateway._pending


async def test_gateway_publish_exception_has_unknown_outcome():
    gateway, _ = _fake_gateway()

    def fail_publish(*args, **kwargs):
        raise OSError('connection closed')
    gateway._client.publish = fail_publish
    with pytest.raises(GatewayResultUnknown):
        await gateway.run_action_group('42')
    assert not gateway._pending


async def test_invalid_payload_does_not_register_or_publish_request():
    gateway, publications = _fake_gateway()
    with pytest.raises(TypeError):
        await gateway.request('proxy/test', {'value': object()})
    assert not gateway._pending
    assert not publications


@pytest.mark.parametrize('reply', ['[]', 'null', '"bad"', '1', 'not-json'])
async def test_invalid_reply_is_unknown_and_cleans_pending_request(reply):
    gateway, _ = _fake_gateway()

    def publish(topic, packet, qos):
        message_id, _ = decode_message(packet)
        gateway._resolve(message_id, reply)
        return SimpleNamespace(rc=0)

    gateway._client.publish = publish
    with pytest.raises(GatewayResultUnknown):
        await gateway.request('proxy/test', {})
    assert not gateway._pending


def test_gateway_ssl_context_keeps_certificate_verification():
    context = Mock(verify_flags=ssl.VERIFY_X509_STRICT)
    with patch('custom_components.xiaomi_miot.core.local_gateway.ssl.create_default_context', return_value=context) as factory:
        assert gateway_ssl_context('ca.pem', 'client.pem', 'key.pem') is context
    factory.assert_called_once_with(cafile='ca.pem')
    context.load_cert_chain.assert_called_once_with('client.pem', 'key.pem')
    assert not context.verify_flags & ssl.VERIFY_X509_STRICT
    # No disabling of verify_mode or hostname checking is permitted.
    assert 'verify_mode' not in context.__dict__
    assert 'check_hostname' not in context.__dict__


async def test_closed_client_cannot_restart_or_publish():
    gateway, publications = _fake_gateway()
    await gateway.close()
    await gateway.close()
    with pytest.raises(GatewayUnavailable):
        await gateway.connect()
    gateway._ready.set()  # A stale subscription must not bypass closure.
    with pytest.raises(GatewayUnavailable):
        await gateway.run_action_group('42')
    assert not publications


async def test_late_subscription_cannot_restore_disconnected_readiness():
    gateway, _ = _fake_gateway()
    gateway._connected = False
    gateway._disconnected()
    gateway._subscribed()
    assert not gateway._ready.is_set()
    gateway._connected = True
    await gateway.close()
    gateway._subscribed()
    assert not gateway._ready.is_set()
