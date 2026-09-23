"""Offline checks for the independent central-gateway wire protocol."""

import asyncio
import json
import struct
from types import SimpleNamespace

import pytest

from custom_components.xiaomi_miot.core.local_gateway import (
    GatewayResultUnknown,
    GatewayUnavailable,
    LocalGatewayClient,
    decode_message,
    encode_message,
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
    publications = []

    def publish(topic, packet, qos):
        publications.append((topic, packet, qos))
        if publish_rc == 0:
            message_id, _ = decode_message(packet)
            gateway._resolve(message_id, json.dumps({'result': {'code': 1}}))
        return SimpleNamespace(rc=publish_rc)

    gateway._client = SimpleNamespace(publish=publish)
    return gateway, publications


def test_gateway_request_uses_local_topic_and_correlates_reply():
    async def run():
        gateway, publications = _fake_gateway()
        result = await gateway.run_action_group('42')

        assert result == {'code': 1}
        assert publications[0][0] == 'master/proxy/execMijiaActionGroup'
        assert publications[0][2] == 2
        assert b'"id":"42"' in publications[0][1]
        assert not gateway._pending

    asyncio.run(run())


def test_gateway_publish_rejection_is_pre_dispatch_failure():
    async def run():
        gateway, _ = _fake_gateway(publish_rc=4)
        with pytest.raises(GatewayUnavailable):
            await gateway.run_action_group('42')
        assert not gateway._pending

    asyncio.run(run())


def test_gateway_timeout_has_unknown_outcome():
    async def run():
        gateway, _ = _fake_gateway()
        gateway._client.publish = lambda *args, **kwargs: SimpleNamespace(rc=0)
        with pytest.raises(GatewayResultUnknown):
            await gateway.request('proxy/execMijiaActionGroup', {'id': '42'},
                                  timeout=0.001)
        assert not gateway._pending

    asyncio.run(run())


def test_gateway_publish_exception_has_unknown_outcome():
    async def run():
        gateway, _ = _fake_gateway()
        def fail_publish(*args, **kwargs):
            raise OSError('connection closed')
        gateway._client.publish = fail_publish
        with pytest.raises(GatewayResultUnknown):
            await gateway.run_action_group('42')
        assert not gateway._pending

    asyncio.run(run())
