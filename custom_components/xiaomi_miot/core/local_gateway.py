"""Independent transport for requests to a Xiaomi central gateway.

Credentials and gateway discovery are deliberately supplied by the caller.
This module does not use another Home Assistant integration's runtime state.
"""

import asyncio
import json
import secrets
import ssl
import struct

import paho.mqtt.client as mqtt


class GatewayUnavailable(Exception):
    """The request was not sent to a connected gateway."""


class GatewayResultUnknown(Exception):
    """A sent request has no trustworthy completion result."""


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
        if kind == 0 and length == 4:
            message_id = int.from_bytes(value, 'little')
        elif kind == 2:
            payload = value.rstrip(b'\0').decode('utf-8')
    if message_id is None or payload is None:
        raise ValueError('gateway reply missing id or payload')
    return message_id, payload


class LocalGatewayClient:
    """A reusable mTLS/MQTT connection scoped to one gateway identity."""

    def __init__(self, host, port, did, ca_file, cert_file, key_file):
        self._loop = asyncio.get_running_loop()
        self._reply_topic = f'{did}/reply'
        self._ready = asyncio.Event()
        self._connected = False
        self._pending = {}
        self._next_id = secrets.randbits(32)
        self._started = False

        context = ssl.create_default_context(cafile=ca_file)
        context.load_cert_chain(cert_file, key_file)
        # Xiaomi's CA predates OpenSSL's strict basic-constraints check.
        # Keep normal certificate and hostname verification enabled.
        context.verify_flags &= ~getattr(ssl, 'VERIFY_X509_STRICT', 0)
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
        if reason_code.is_failure:
            return
        self._connected = True
        client.subscribe(self._reply_topic, qos=2)

    def _on_subscribe(self, client, userdata, mid, reason_codes, properties):
        if self._connected and all(not code.is_failure for code in reason_codes):
            self._loop.call_soon_threadsafe(self._ready.set)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        self._connected = False
        self._loop.call_soon_threadsafe(self._disconnected)

    def _disconnected(self):
        self._ready.clear()
        for future in self._pending.values():
            if not future.done():
                future.set_exception(GatewayResultUnknown('gateway disconnected'))
        self._pending.clear()

    def _on_message(self, client, userdata, message):
        if message.topic != self._reply_topic:
            return
        try:
            message_id, payload = decode_message(message.payload)
        except (UnicodeDecodeError, ValueError):
            return
        self._loop.call_soon_threadsafe(self._resolve, message_id, payload)

    def _resolve(self, message_id, payload):
        future = self._pending.pop(message_id, None)
        if future and not future.done():
            future.set_result(payload)

    async def connect(self, timeout=10):
        """Wait until the reply subscription is active."""
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

    async def request(self, topic: str, payload: dict, timeout=10) -> dict:
        """Send a gateway request; a post-publish timeout is not retryable."""
        if not self._ready.is_set():
            raise GatewayUnavailable('gateway is not connected')
        self._next_id = (self._next_id + 1) & 0xffffffff
        while self._next_id in self._pending:
            self._next_id = (self._next_id + 1) & 0xffffffff
        message_id = self._next_id
        future = self._loop.create_future()
        self._pending[message_id] = future
        packet = encode_message(
            message_id, json.dumps(payload, separators=(',', ':')),
            self._reply_topic,
        )
        try:
            result = self._client.publish(f'master/{topic}', packet, qos=2)
        except (OSError, ValueError) as exc:
            self._pending.pop(message_id, None)
            raise GatewayResultUnknown('gateway publish outcome unknown') from exc
        if result.rc != mqtt.MQTT_ERR_SUCCESS:
            self._pending.pop(message_id, None)
            raise GatewayUnavailable('gateway publish rejected')
        try:
            return json.loads(await asyncio.wait_for(future, timeout))
        except (TimeoutError, UnicodeDecodeError, ValueError) as exc:
            raise GatewayResultUnknown('gateway reply unavailable') from exc
        finally:
            self._pending.pop(message_id, None)

    async def action_groups(self) -> list[str]:
        reply = await self.request('proxy/getMijiaActionGroupList', {})
        groups = reply.get('result') if isinstance(reply, dict) else None
        if not isinstance(groups, list):
            raise GatewayUnavailable('gateway action groups unavailable')
        return [str(group) for group in groups]

    async def run_action_group(self, scene_id: str) -> dict:
        reply = await self.request('proxy/execMijiaActionGroup', {
            'id': str(scene_id),
        })
        result = reply.get('result') if isinstance(reply, dict) else None
        if not isinstance(result, dict):
            raise GatewayResultUnknown('gateway execution result unavailable')
        return result

    async def close(self):
        """Stop the MQTT worker and fail any pending requests."""
        self._disconnected()
        if self._started:
            self._client.disconnect()
            await self._loop.run_in_executor(None, self._client.loop_stop)
            self._started = False
