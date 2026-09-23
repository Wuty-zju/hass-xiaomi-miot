"""Account-scoped, independent Xiaomi central-gateway scene transport."""

import asyncio
from datetime import datetime, timedelta, timezone
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
    GatewayUnavailable,
    LocalGatewayClient,
)


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
            credentials = self._credentials
            if (not credentials
                    or str(credentials.get('uid')) != str(self.cloud.user_id)
                    or credentials.get('region') != self.cloud.default_server):
                raise GatewayUnavailable('gateway OAuth account is not configured')
            session = async_get_clientsession(self.hass)
            if credentials['expires_at'] <= time.time():
                token = await exchange_token(
                    session, credentials['region'], OAUTH_CLIENT_ID,
                    credentials['redirect_uri'], credentials['oauth_device_id'],
                    refresh_token=credentials['refresh_token'],
                )
                credentials.update(token)
                await self._store.async_save(credentials)

            certificate = credentials.get('certificate')
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
            credentials.update(private_key=private_key, certificate=certificate)
            await self._store.async_save(credentials)
            for client in self._clients.values():
                await client.close()
            self._clients.clear()

    async def _client_for(self, address):
        key = (address.group_id, address.host, address.port)
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
        client = LocalGatewayClient(
            address.host, address.port, credentials['virtual_did'],
            str(Path(__file__).with_name('mijia_gateway_ca.pem')),
            cert_file, key_file,
        )
        self._clients[key] = client
        return client

    async def run_scene(self, scene) -> bool:
        """Try local execution; raise before dispatch if cloud fallback is safe."""
        if not self._discovery:
            raise GatewayUnavailable('gateway OAuth is not configured')
        group_id = home_group_id(str(scene['owner_uid']), str(scene['home_id']))
        address = await self._discovery.wait_for_group(group_id)
        if not address:
            raise GatewayUnavailable('no central gateway found for scene home')
        try:
            await self._ensure_certificate()
        except (GatewayAuthorizationError, aiohttp.ClientError,
                TimeoutError, ValueError, KeyError) as exc:
            raise GatewayUnavailable('gateway credentials unavailable') from exc
        scene_id = str(scene['scene_id'])
        try:
            client = await self._client_for(address)
            await client.connect()
            groups = await client.action_groups()
        except (GatewayUnavailable, GatewayResultUnknown, OSError) as exc:
            raise GatewayUnavailable('gateway scene list unavailable') from exc
        if scene_id not in groups:
            raise GatewayUnavailable('scene not available on the local gateway')
        result = await client.run_action_group(scene_id)
        if result.get('code') in (0, 1):
            return True
        raise GatewayResultUnknown('gateway did not confirm scene execution')
