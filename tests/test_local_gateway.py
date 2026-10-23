"""Offline checks for the independent central-gateway wire protocol."""

import asyncio
import json
import struct
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
import ssl
from threading import RLock

import pytest
import paho.mqtt.client as mqtt

from custom_components.xiaomi_miot.core.local_gateway import (
    GatewayResultUnknown,
    GatewayRejected,
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
    gateway._started = True
    gateway._connected = True
    gateway._state_lock = RLock()
    gateway._generation = gateway._ready_generation = 1
    gateway._subscription_mid = 7
    gateway._on_state = None
    publications = []

    def publish(topic, packet, qos):
        publications.append((topic, packet, qos))
        if publish_rc == 0:
            message_id, _ = decode_message(packet)
            gateway._resolve(gateway.session, message_id, json.dumps({'result': {'code': 1}}))
        return SimpleNamespace(rc=publish_rc)

    gateway._client = SimpleNamespace(publish=publish, disconnect=Mock(), loop_stop=Mock())
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
        gateway._resolve(gateway.session, message_id, reply)
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
    gateway._on_disconnect(gateway._client, None, None, None, None)
    gateway._state_changed(gateway._generation)
    gateway._subscribed(1, 7, True)
    assert not gateway._ready.is_set()
    await gateway.close()
    gateway._subscribed(gateway._generation, 7, True)
    assert not gateway._ready.is_set()


@pytest.mark.parametrize('codes', [[], [SimpleNamespace(is_failure=True)],
                                  [SimpleNamespace(is_failure=False)] * 2])
async def test_empty_or_failed_suback_cannot_grant_readiness(codes):
    gateway, _ = _fake_gateway()
    gateway._ready.clear()
    gateway._ready_generation = None
    gateway._on_subscribe(gateway._client, None, 7, codes, None)
    await asyncio.sleep(0)
    assert gateway.session is None and not gateway._ready.is_set()


async def test_suback_requires_matching_mid_and_connection_generation():
    gateway, _ = _fake_gateway()
    gateway._ready_generation = None
    gateway._ready.clear()
    gateway._subscribed(1, 8, True)
    assert gateway.session is None
    gateway._subscribed(1, 7, True)
    assert gateway.session == 1
    # A callback already queued by the old worker cannot confirm the new session.
    gateway._on_disconnect(gateway._client, None, None, None, None)
    gateway._client.subscribe = Mock(return_value=(0, 9))
    gateway._on_connect(gateway._client, None, None, SimpleNamespace(is_failure=False), None)
    gateway._subscribed(1, 7, True)
    await asyncio.sleep(0)
    assert gateway.session is None and not gateway._ready.is_set()
    gateway._subscribed(gateway._generation, 9, True)
    assert gateway.session == gateway._generation


async def test_failed_subscribe_submission_never_becomes_ready():
    gateway, _ = _fake_gateway()
    gateway._client.subscribe = Mock(return_value=(4, 9))
    gateway._on_connect(gateway._client, None, None, SimpleNamespace(is_failure=False), None)
    gateway._subscribed(gateway._generation, 9, True)
    await asyncio.sleep(0)
    assert gateway.session is None


async def test_connection_and_reply_share_one_timeout_budget():
    gateway, _ = _fake_gateway()
    gateway._client.publish = Mock(return_value=SimpleNamespace(rc=0))
    clock = SimpleNamespace(now=10)
    gateway._loop = SimpleNamespace(
        time=lambda: clock.now, create_future=asyncio.get_running_loop().create_future,
    )

    async def connect(timeout):
        clock.now += 0.03

    gateway.connect = connect
    with patch('custom_components.xiaomi_miot.core.local_gateway.asyncio.wait_for',
               AsyncMock(side_effect=TimeoutError)) as wait:
        with pytest.raises(GatewayResultUnknown):
            await gateway.request('proxy/test', {}, timeout=0.04)
    assert wait.call_args.args[1] == pytest.approx(0.01)
    assert not gateway._pending


async def test_expired_budget_or_changed_qualification_never_publishes():
    gateway, publications = _fake_gateway()
    with pytest.raises(GatewayUnavailable):
        await gateway.request('proxy/test', {}, session=0)
    gateway.connect = AsyncMock()
    with pytest.raises(GatewayUnavailable):
        await gateway.request('proxy/test', {}, timeout=0)
    assert not publications and not gateway._pending


async def test_cancelled_request_and_late_reply_do_not_replay_or_leak():
    gateway, _ = _fake_gateway()
    sent = asyncio.Event()
    message_ids = []

    def publish(topic, packet, qos):
        message_ids.append(decode_message(packet)[0])
        sent.set()
        return SimpleNamespace(rc=0)

    gateway._client.publish = publish
    request = asyncio.create_task(gateway.request('proxy/test', {}))
    await sent.wait()
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    gateway._resolve(1, message_ids[0], '{"result":{}}')
    assert not gateway._pending and len(message_ids) == 1


async def test_disconnect_fails_sent_request_and_old_reply_cannot_resolve_new_session():
    gateway, _ = _fake_gateway()
    sent = asyncio.Event()
    gateway._client.publish = lambda *args, **kwargs: (sent.set() or SimpleNamespace(rc=0))
    request = asyncio.create_task(gateway.request('proxy/test', {}))
    await sent.wait()
    message_id = next(iter(gateway._pending))
    gateway._on_disconnect(gateway._client, None, None, None, None)
    gateway._resolve(1, message_id, '{"result":{}}')
    with pytest.raises(GatewayResultUnknown):
        await request
    assert not gateway._pending


@pytest.mark.parametrize('reply', [
    {'error': None, 'result': ['42']}, {'code': False, 'result': ['42']},
    {'code': 1, 'result': ['42']}, {'result': [True]}, {'result': [{}]},
    {'result': ['']}, {'result': None},
])
async def test_action_group_snapshot_rejects_error_envelope_and_invalid_ids(reply):
    gateway, _ = _fake_gateway()
    # Exercise transport envelope parsing as well as the list parser.
    def publish(topic, packet, qos):
        gateway._resolve(gateway.session, decode_message(packet)[0], json.dumps(reply))
        return SimpleNamespace(rc=0)
    gateway._client.publish = publish
    with pytest.raises(GatewayResultUnknown):
        await gateway.action_groups()


async def test_action_group_ids_keep_raw_values_and_deduplicate():
    gateway, _ = _fake_gateway()
    gateway.request = AsyncMock(return_value={'result': ['42', 43, '42']})
    assert await gateway.action_groups() == ['42', '43']


@pytest.mark.parametrize('code', [None, False, True, '1', -1])
async def test_action_result_is_typed_and_rejection_is_distinct(code):
    gateway, _ = _fake_gateway()
    gateway.request = AsyncMock(return_value={'result': {'code': code}})
    error = GatewayRejected if code == -1 else GatewayResultUnknown
    with pytest.raises(error):
        await gateway.run_action_group('42')


async def test_nested_error_is_not_success_even_with_success_code():
    gateway, _ = _fake_gateway()
    gateway.request = AsyncMock(return_value={'result': {'code': 1, 'error': None}})
    with pytest.raises(GatewayResultUnknown):
        await gateway.run_action_group('42')


@pytest.mark.parametrize('field', [
    struct.pack('<IBI', 4, 0, 99), struct.pack('<IB', 3, 2) + b'{}\0',
])
def test_gateway_message_rejects_ambiguous_duplicate_fields(field):
    with pytest.raises(ValueError):
        decode_message(encode_message(23, '{}', 'virtual/reply') + field)
