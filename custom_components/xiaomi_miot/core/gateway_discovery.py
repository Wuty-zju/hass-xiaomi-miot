"""Discover Xiaomi central gateways without relying on another integration."""

import asyncio
import base64
from dataclasses import dataclass
import hashlib
import time

from zeroconf import IPVersion, ServiceStateChange
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo


SERVICE_TYPE = '_miot-central._tcp.local.'


def home_group_id(owner_uid: str, home_id: str) -> str:
    """Derive the local MQTT group advertised for a Xiaomi home."""
    value = f'{owner_uid}central_service{home_id}'.encode()
    return hashlib.sha1(value).hexdigest()[:16]


@dataclass(frozen=True)
class GatewayAddress:
    """A central gateway's advertised identity and endpoint."""

    did: str
    group_id: str
    host: str
    port: int


def parse_gateway_service(info) -> GatewayAddress | None:
    """Accept only a central gateway advertising MQTT capability."""
    encoded = info.properties.get(b'profile')
    if not encoded:
        return None
    try:
        profile = base64.b64decode(encoded, validate=True)
    except (ValueError, base64.binascii.Error):
        return None
    if len(profile) < 23 or profile[20] >> 4 != 1 or not profile[22] & 2:
        return None
    addresses = info.parsed_addresses(IPVersion.V4Only)
    if not addresses or not info.port:
        return None
    return GatewayAddress(
        did=str(int.from_bytes(profile[1:9], 'big')),
        group_id=profile[9:17][::-1].hex(),
        host=addresses[0],
        port=info.port,
    )


class GatewayDiscovery:
    """Maintain a read-only registry of visible LAN gateway advertisements."""

    def __init__(self, zeroconf):
        self._zeroconf = zeroconf
        self._browser = None
        self._by_service = {}
        self._changed = asyncio.Event()

    async def start(self):
        if self._browser is None:
            self._browser = AsyncServiceBrowser(
                self._zeroconf, SERVICE_TYPE,
                handlers=[self._service_changed],
            )

    async def close(self):
        if self._browser is not None:
            await self._browser.async_cancel()
            self._browser = None
        self._by_service.clear()

    def _service_changed(self, zeroconf, service_type, name, state):
        if state == ServiceStateChange.Removed:
            self._by_service.pop(name, None)
            self._changed.set()
        else:
            asyncio.create_task(self._refresh(service_type, name))

    async def _refresh(self, service_type, name):
        info = AsyncServiceInfo(service_type, name)
        if await info.async_request(self._zeroconf, timeout=3000):
            address = parse_gateway_service(info)
            if address is not None:
                self._by_service[name] = address
                self._changed.set()

    def for_group(self, group_id: str) -> GatewayAddress | None:
        """Find a gateway for one exact home group."""
        return next((item for item in self._by_service.values()
                     if item.group_id == group_id), None)

    async def wait_for_group(self, group_id: str, timeout: float = 2
                             ) -> GatewayAddress | None:
        """Allow a newly started browser time to receive an advertisement."""
        deadline = time.monotonic() + timeout
        while True:
            self._changed.clear()
            if address := self.for_group(group_id):
                return address
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                await asyncio.wait_for(self._changed.wait(), remaining)
            except TimeoutError:
                return None

    @property
    def gateways(self) -> tuple[GatewayAddress, ...]:
        return tuple(self._by_service.values())
