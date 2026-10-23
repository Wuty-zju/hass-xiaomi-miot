"""Independent transport for requests to a Xiaomi central gateway.

Credentials and gateway discovery are deliberately supplied by the caller.
This module does not use another Home Assistant integration's runtime state.
"""

import asyncio
import json
import secrets
import ssl
import struct
from threading import RLock

import paho.mqtt.client as mqtt
from .scene_gateway import GatewayUnavailable, GatewayResultUnknown, GatewayRejected


def _scene_id(value):
    if type(value) not in (str, int) or not str(value).strip():
        raise GatewayResultUnknown('gateway scene identifier is invalid')
    return str(value)


def encode_message(message_id: int, payload: str, reply_topic: str) -> bytes:
    """Encode the gateway's length-prefixed MQTT request fields."""
    fields = (
        (0, message_id.to_bytes(4, 'little')),
        (3, b'local\0'),
        (1, reply_topic.encode() + b'\0'),
        (2, payload.encode() + b'\0'),
    )
    return b''.join(struct.pack('<IB', len(value), kind) + value
                    for kind, value in fields)


def decode_message(data: bytes) -> tuple[int, str]:
    """Read an MQTT reply without trusting its lengths or field order."""
    message_id = None
    payload = None
    offset = 0
    while offset < len(data):
        if len(data) - offset < 5:
            raise ValueError('truncated gateway field header')
        length, kind = struct.unpack_from('<IB', data, offset)
        offset += 5
        if length > len(data) - offset:
            raise ValueError('truncated gateway field')
        value = data[offset:offset + length]
        offset += length
        if kind == 0:
            if length != 4 or message_id is not None:
                raise ValueError('invalid gateway message id field')
            message_id = int.from_bytes(value, 'little')
        elif kind == 2:
            if payload is not None:
                raise ValueError('duplicate gateway payload field')
            payload = value.rstrip(b'\0').decode('utf-8')
    if message_id is None or payload is None:
        raise ValueError('gateway reply missing id or payload')
    return message_id, payload


def gateway_ssl_context(ca_file, cert_file, key_file) -> ssl.SSLContext:
    """Load gateway credentials in an executor, not the event loop."""
    context = ssl.create_default_context(cafile=ca_file)
    context.load_cert_chain(cert_file, key_file)
    # Xiaomi's CA predates OpenSSL's strict basic-constraints check.
    # Keep normal certificate and hostname verification enabled.
    context.verify_flags &= ~getattr(ssl, 'VERIFY_X509_STRICT', 0)
    return context


class LocalGatewayClient:
    """A reusable mTLS/MQTT connection scoped to one gateway identity."""

    def __init__(self, host, port, did, context: ssl.SSLContext, on_state=None):
        self._loop = asyncio.get_running_loop()
        self._reply_topic = f'{did}/reply'
        self._ready = asyncio.Event()
        self._connected = False
        self._pending = {}
        self._next_id = secrets.randbits(32)
        self._started = False
        self._closed = False
        # Paho callbacks run on its worker; publish and connection revocation
        # must agree on the same generation without an intervening await.
        self._state_lock = RLock()
        self._generation = 0
        self._subscription_mid = None
        self._ready_generation = None
        self._on_state = on_state

        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=did,
            protocol=mqtt.MQTTv5,
        )
        self._client.tls_set_context(context)
        self._client.on_connect = self._on_connect
        self._client.on_subscribe = self._on_subscribe
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message
        self._client.connect_async(host, port, keepalive=60)

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        with self._state_lock:
            if self._closed:
                return
            self._generation += 1
            generation = self._generation
            self._ready_generation = self._subscription_mid = None
            self._connected = not reason_code.is_failure
            if self._connected:
                result, mid = client.subscribe(self._reply_topic, qos=2)
                if result == mqtt.MQTT_ERR_SUCCESS:
                    self._subscription_mid = mid
        self._loop.call_soon_threadsafe(self._state_changed, generation)

    def _on_subscribe(self, client, userdata, mid, reason_codes, properties):
        with self._state_lock:
            generation = self._generation
        accepted = (isinstance(reason_codes, (list, tuple)) and len(reason_codes) == 1
                    and not reason_codes[0].is_failure)
        self._loop.call_soon_threadsafe(self._subscribed, generation, mid, accepted)

    def _subscribed(self, generation, mid, accepted):
        with self._state_lock:
            if (self._closed or not self._connected or generation != self._generation
                    or mid != self._subscription_mid or self._subscription_mid is None):
                return
            self._ready_generation = generation if accepted else None
        if accepted:
            self._ready.set()
        else:
            self._ready.clear()
        if self._on_state:
            self._on_state()

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        with self._state_lock:
            self._connected = False
            self._generation += 1
            generation = self._generation
            self._ready_generation = self._subscription_mid = None
        self._loop.call_soon_threadsafe(self._state_changed, generation)

    def _state_changed(self, generation):
        if generation == self._generation and self.session is None:
            self._ready.clear()
        for message_id, (sent_generation, future) in list(self._pending.items()):
            if sent_generation != self._generation:
                self._pending.pop(message_id, None)
                if not future.done():
                    future.set_exception(GatewayResultUnknown('gateway disconnected'))
        if generation == self._generation and self._on_state:
            self._on_state()

    @property
    def session(self):
        """Only a confirmed current subscription grants execution readiness."""
        with self._state_lock:
            if (not self._closed and self._connected
                    and self._ready_generation == self._generation):
                return self._generation
        return None

    def watch_state(self, callback):
        """One home worker observes this owned connection on the HA loop."""
        self._on_state = callback

        def unsubscribe():
            if self._on_state is callback:
                self._on_state = None

        return unsubscribe

    def _on_message(self, client, userdata, message):
        if message.topic != self._reply_topic:
            return
        try:
            message_id, payload = decode_message(message.payload)
        except (UnicodeDecodeError, ValueError):
            return
        with self._state_lock:
            generation = self._generation
        self._loop.call_soon_threadsafe(self._resolve, generation, message_id, payload)

    def _resolve(self, generation, message_id, payload):
        pending = self._pending.get(message_id)
        if pending and pending[0] == generation == self.session:
            _, future = self._pending.pop(message_id)
            if not future.done():
                future.set_result(payload)

    async def connect(self, timeout=10):
        """Wait until the reply subscription is active."""
        if self._closed or timeout <= 0:
            raise GatewayUnavailable('gateway client is closed')
        if not self._started:
            try:
                self._client.loop_start()
            except RuntimeError as exc:
                raise GatewayUnavailable('gateway MQTT worker unavailable') from exc
            self._started = True
        try:
            await asyncio.wait_for(self._ready.wait(), timeout)
        except TimeoutError as exc:
            raise GatewayUnavailable('gateway connection unavailable') from exc
        if self.session is None:
            raise GatewayUnavailable('gateway subscription is unavailable')

    async def request(self, topic: str, payload: dict, timeout=10, *, session=None) -> dict:
        """One budget for connection and reply; no retry after publication."""
        deadline = self._loop.time() + timeout
        data = json.dumps(payload, separators=(',', ':'))
        await self.connect(timeout)
        self._next_id = (self._next_id + 1) & 0xffffffff
        while self._next_id in self._pending:
            self._next_id = (self._next_id + 1) & 0xffffffff
        message_id = self._next_id
        packet = encode_message(
            message_id, data,
            self._reply_topic,
        )
        future = self._loop.create_future()
        try:
            try:
                with self._state_lock:
                    current = self.session
                    if (current is None or (session is not None and current != session)
                            or self._loop.time() >= deadline):
                        raise GatewayUnavailable('gateway session changed before dispatch')
                    self._pending[message_id] = (current, future)
                    result = self._client.publish(f'master/{topic}', packet, qos=2)
            except (OSError, ValueError) as exc:
                raise GatewayResultUnknown('gateway publish outcome unknown') from exc
            if result.rc != mqtt.MQTT_ERR_SUCCESS:
                # QoS 2 messages may remain queued even on MQTT_ERR_NO_CONN.
                # A subsequent cloud execution could duplicate the command.
                raise GatewayResultUnknown('gateway publish outcome unknown')
            try:
                reply = json.loads(await asyncio.wait_for(future, max(0, deadline - self._loop.time())))
            except (TimeoutError, UnicodeDecodeError, ValueError) as exc:
                raise GatewayResultUnknown('gateway reply unavailable') from exc
            if (not isinstance(reply, dict) or 'error' in reply
                    or ('code' in reply and (type(reply['code']) is not int or reply['code'] != 0))):
                raise GatewayResultUnknown('gateway reply envelope is invalid')
            return reply
        finally:
            self._pending.pop(message_id, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()  # Observe a disconnect racing a failed publish.

    async def action_groups(self, timeout=10) -> list[str]:
        reply = await self.request('proxy/getMijiaActionGroupList', {}, timeout)
        groups = reply.get('result') if isinstance(reply, dict) else None
        if not isinstance(groups, list):
            raise GatewayResultUnknown('gateway action groups unavailable')
        return list(dict.fromkeys(_scene_id(group) for group in groups))

    async def run_action_group(self, scene_id: str, timeout=10, *, session=None) -> dict:
        reply = await self.request('proxy/execMijiaActionGroup', {
            'id': _scene_id(scene_id),
        }, timeout, session=session)
        result = reply.get('result') if isinstance(reply, dict) else None
        if (not isinstance(result, dict) or 'error' in result
                or type(result.get('code')) is not int):
            raise GatewayResultUnknown('gateway execution result unavailable')
        if result['code'] not in (0, 1):
            raise GatewayRejected('gateway rejected scene execution')
        return result

    async def close(self):
        """Stop the MQTT worker and fail any pending requests."""
        if self._closed:
            return
        with self._state_lock:
            self._closed = True
            self._connected = False
            self._generation += 1
            self._ready_generation = self._subscription_mid = None
        self._state_changed(self._generation)
        if self._started:
            self._client.disconnect()
            await self._loop.run_in_executor(None, self._client.loop_stop)
            self._started = False
