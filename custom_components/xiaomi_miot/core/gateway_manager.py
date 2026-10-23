"""Account-scoped, independent Xiaomi central-gateway scene transport."""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
import os
from pathlib import Path
import tempfile
import time

import aiohttp
from homeassistant.components import zeroconf
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store

from .gateway_auth import (
    GatewayAuthorizationError,
    OAUTH_CLIENT_ID,
    exchange_token,
    generate_certificate_request,
    issue_gateway_certificate,
    validate_gateway_certificate,
)
from .gateway_discovery import GatewayDiscovery, home_group_id
from .local_gateway import (
    GatewayResultUnknown,
    GatewayRejected,
    GatewayUnavailable,
    LocalGatewayClient,
    gateway_ssl_context,
)

_LOGGER = logging.getLogger(__name__)


def _home_scope(scene):
    if not isinstance(scene, dict):
        raise GatewayUnavailable('invalid gateway home scope')
    values = tuple(scene.get(key) for key in ('owner_uid', 'home_id'))
    if any(type(value) not in (str, int) or not str(value).strip() for value in values):
        raise GatewayUnavailable('invalid gateway home scope')
    return tuple(str(value) for value in values)


@dataclass(frozen=True)
class GatewaySceneSnapshot:
    """Ephemeral authorization from one exact gateway connection, never stored."""

    manager: 'GatewayManager'
    client: LocalGatewayClient
    address: object
    account: tuple
    scope: tuple
    session: int
    groups: frozenset
    expires: float

    def watch(self, callback):
        return self.client.watch_state(callback)

    def available(self, scene):
        manager = self.manager
        try:
            scope = _home_scope(scene)
        except GatewayUnavailable:
            return False
        return (scope == self.scope and type(scene.get('scene_id')) in (str, int)
                and str(scene['scene_id']) in self.groups
                and not manager._closed and manager._discovery is not None
                and manager.account == self.account
                and manager.hass.loop.time() < self.expires
                and manager._discovery.for_group(self.address.group_id) == self.address
                and self.client.session == self.session)

    async def run(self, scene):
        """No discovery, certificate or list I/O is permitted at dispatch."""
        if not self.available(scene):
            raise GatewayUnavailable('gateway qualification is no longer current')
        result = await self.client.run_action_group(str(scene['scene_id']), session=self.session)
        if (not isinstance(result, dict) or 'error' in result
                or type(result.get('code')) is not int):
            raise GatewayResultUnknown('gateway execution response is invalid')
        if result['code'] not in (0, 1):
            raise GatewayRejected('gateway rejected scene execution')
        return True


def gateway_store(hass, entry_id):
    """Persist this entry's separate OAuth grant and DID-bound certificate."""
    return Store(hass, 1, f'xiaomi_miot_gateway_{entry_id}',
                 private=True, atomic_writes=True)


def _write_client_credentials(directory: str, certificate: str,
                              private_key: str) -> tuple[str, str]:
    path = Path(directory)
    cert_file = path / 'client.crt'
    key_file = path / 'client.key'
    cert_file.write_text(certificate)
    descriptor = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, 'w') as handle:
        handle.write(private_key)
    return str(cert_file), str(key_file)


class GatewayManager:
    """Route one account's scene requests through its own LAN connections."""

    def __init__(self, hass, entry_id, cloud):
        self.hass = hass
        self.cloud = cloud
        self._store = gateway_store(hass, entry_id)
        self._credentials = None
        self._discovery = None
        self._clients = {}
        self._directory = None
        self._credential_lock = asyncio.Lock()
        self._closed = False

    @property
    def account(self):
        return str(self.cloud.user_id), self.cloud.default_server

    async def start(self):
        self._credentials = await self._store.async_load()
        if not self._credentials:
            return
        self._directory = await self.hass.async_add_executor_job(
            lambda: tempfile.TemporaryDirectory(prefix='xiaomi_miot_gateway_'),
        )
        aiozc = await zeroconf.async_get_async_instance(self.hass)
        self._discovery = GatewayDiscovery(aiozc.zeroconf)
        await self._discovery.start()

    async def close(self):
        # Revoke snapshots before waiting for credential/client cleanup.
        self._closed = True
        async with self._credential_lock:
            await self._close()

    async def _close(self):
        for client in self._clients.values():
            await client.close()
        self._clients.clear()
        if self._discovery:
            await self._discovery.close()
            self._discovery = None
        if self._directory:
            await self.hass.async_add_executor_job(self._directory.cleanup)
            self._directory = None

    async def _ensure_certificate(self):
        async with self._credential_lock:
            if self._closed:
                raise GatewayUnavailable('gateway manager is closed')
            credentials = self._credentials
            if (not credentials
                    or str(credentials.get('uid')) != str(self.cloud.user_id)
                    or credentials.get('region') != self.cloud.default_server):
                raise GatewayUnavailable('gateway OAuth account is not configured')
            certificate = credentials.get('certificate')
            expires = None
            if certificate:
                try:
                    expires = validate_gateway_certificate(
                        certificate, str(credentials['uid']),
                        credentials['virtual_did'],
                    )
                    if expires > datetime.now(timezone.utc) + timedelta(days=3):
                        return
                except GatewayAuthorizationError:
                    pass
            session = async_get_clientsession(self.hass)
            try:
                if credentials['expires_at'] <= time.time():
                    token = await exchange_token(
                        session, credentials['region'], OAUTH_CLIENT_ID,
                        credentials['redirect_uri'], credentials['oauth_device_id'],
                        refresh_token=credentials['refresh_token'],
                    )
                    credentials.update(token)
                    await self._store.async_save(credentials)
                private_key, csr = generate_certificate_request(
                    str(credentials['uid']), credentials['virtual_did'],
                    credentials.get('private_key'),
                )
                certificate = await issue_gateway_certificate(
                    session, credentials['region'], OAUTH_CLIENT_ID,
                    credentials['access_token'], csr,
                )
                validate_gateway_certificate(
                    certificate, str(credentials['uid']), credentials['virtual_did'],
                )
            except (GatewayAuthorizationError, aiohttp.ClientError, TimeoutError):
                if expires and expires > datetime.now(timezone.utc):
                    _LOGGER.debug('Gateway certificate renewal unavailable; using valid certificate')
                    return
                raise
            credentials.update(private_key=private_key, certificate=certificate)
            await self._store.async_save(credentials)
            for client in self._clients.values():
                await client.close()
            self._clients.clear()

    async def _client_for(self, address):
        # Coordinate creation with credential rotation and unload, including
        # the executor write which otherwise lets concurrent callers race.
        async with self._credential_lock:
            if self._closed or self._directory is None:
                raise GatewayUnavailable('gateway manager is closed')
            return await self._create_client(address)

    async def _create_client(self, address):
        key = (address.group_id, address.did, address.host, address.port)
        client = self._clients.get(key)
        if client:
            return client
        for old_key, old_client in list(self._clients.items()):
            if old_key[0] == address.group_id:
                await old_client.close()
                self._clients.pop(old_key)
        credentials = self._credentials
        cert_file, key_file = await self.hass.async_add_executor_job(
            _write_client_credentials, self._directory.name,
            credentials['certificate'], credentials['private_key'],
        )
        context = await self.hass.async_add_executor_job(
            gateway_ssl_context,
            str(Path(__file__).with_name('mijia_gateway_ca.pem')),
            cert_file, key_file,
        )
        client = LocalGatewayClient(
            address.host, address.port, credentials['virtual_did'], context,
        )
        self._clients[key] = client
        return client

    async def qualify(self, home, timeout=15) -> GatewaySceneSnapshot:
        """Read-only background qualification, bounded across all setup stages."""
        if self._closed or not self._discovery:
            raise GatewayUnavailable('gateway OAuth is not configured')
        scope = _home_scope(home)
        account = self.account
        group_id = home_group_id(*scope)
        deadline = self.hass.loop.time() + timeout
        try:
            async with asyncio.timeout(timeout):
                address = await self._discovery.wait_for_group(group_id)
                if not address:
                    raise GatewayUnavailable('no central gateway found for scene home')
                await self._ensure_certificate()
                client = await self._client_for(address)
                await client.connect(max(0, deadline - self.hass.loop.time()))
                session = client.session
                groups = await client.action_groups(max(0, deadline - self.hass.loop.time()))
                if (self._closed or self.account != account or session is None
                        or client.session != session
                        or self._discovery.for_group(group_id) != address):
                    raise GatewayUnavailable('gateway changed while reading scene list')
        except (GatewayAuthorizationError, aiohttp.ClientError, GatewayResultUnknown,
                TimeoutError, OSError, ValueError, KeyError) as exc:
            raise GatewayUnavailable('gateway scene qualification unavailable') from exc
        return GatewaySceneSnapshot(self, client, address, account, scope, session,
                                    frozenset(groups), self.hass.loop.time() + 600)
