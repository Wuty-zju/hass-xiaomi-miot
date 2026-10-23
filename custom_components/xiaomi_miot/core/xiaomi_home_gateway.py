"""Version-scoped borrowing of Xiaomi Home's live scene transport.

No Xiaomi Home source, credentials or connection ownership is imported. The
private request entry is deliberately isolated and fingerprint-gated: its
generation fence is required, so unknown/older implementations fail closed.
"""

import asyncio
from dataclasses import dataclass
import hashlib
import json
import secrets

from homeassistant.config_entries import ConfigEntryState
from homeassistant.loader import IntegrationNotFound, async_get_integration

from .scene_gateway import (
    GatewayRejected, GatewayResultUnknown, GatewayUnavailable, home_group_id,
)

# Reviewed Wuty-zju/ha_xiaomi_home d740f351, not a promise about all v0.6.x.
VERIFIED_VERSION = 'v0.6.4.3'
VERIFIED_FILES = {
    'miot/miot_mips.py': '4fab9957ee4fb3ff4e89a30ce212695dd2c101a5a348906963f1b96e5f7590cd',
    'miot/miot_client.py': '061bd9f4c51a29893a468797d278eda77f94ce58c529e438ce9869ce4e8a7395',
}


def _fingerprint(path):
    return all(hashlib.sha256((path / name).read_bytes()).hexdigest() == expected
               for name, expected in VERIFIED_FILES.items())


def _id(value):
    if type(value) not in (str, int) or not str(value).strip():
        raise GatewayUnavailable('invalid Xiaomi Home scene identity')
    return str(value)


def _session(local):
    generation = getattr(local, '_reply_generation', None)
    ready_generation = getattr(local, '_reply_ready_generation', None)
    ready = getattr(local, '_reply_ready', None)
    if (type(generation) is int and getattr(local, 'mips_state', False) is True
            and type(ready_generation) is int and generation == ready_generation
            and isinstance(ready, asyncio.Event) and ready.is_set()):
        return generation
    return None


@dataclass(frozen=True)
class HomeSceneSnapshot:
    backend: 'XiaomiHomeGateway'
    entry: object
    client: object
    local: object
    group: str
    route: object
    scope: tuple
    generation: int
    groups: frozenset
    expires: float

    def available(self, scene):
        try:
            scope = _id(scene.get('owner_uid')), _id(scene.get('home_id'))
            scene_id = _id(scene.get('scene_id'))
        except (AttributeError, GatewayUnavailable):
            return False
        current = self.backend._match(*scope)
        route = getattr(self.client, '_scene_routes', {}).get(self.group)
        return (scope == self.scope and scene_id in self.groups
                and not self.backend._closed and self.backend._compatible
                and current is not None and current[0] is self.entry
                and current[1] is self.client and current[2] is self.local
                and route is self.route and route.client is self.local
                and isinstance(route.scene_ids, frozenset) and scene_id in route.scene_ids
                and _session(self.local) == self.generation
                and self.backend.hass.loop.time() < self.expires)

    def watch(self, callback):
        key = f'xiaomi_miot-scene-{secrets.token_hex(12)}'
        self.client.sub_manual_scene_state(key, callback)

        async def state_changed(group, connected):
            callback()

        self.local.sub_mips_state(key, state_changed)
        stop_entry = self.entry.async_on_state_change(callback)

        def unsubscribe():
            self.client.unsub_manual_scene_state(key)
            try:
                self.local.unsub_mips_state(key)
            except KeyError:
                pass  # The owning integration may already have removed it.
            stop_entry()

        return unsubscribe

    async def run(self, scene):
        if not self.available(scene):
            raise GatewayUnavailable('borrowed scene qualification is no longer current')
        # The reviewed transport checks this exact generation again on its
        # worker before publication. Its public exec method cannot accept it.
        reply = await self.backend._request(
            self.local, self.generation, 'proxy/execMijiaActionGroup',
            {'id': _id(scene['scene_id'])},
        )
        result = reply.get('result')
        if (not isinstance(result, dict) or 'error' in result
                or type(result.get('code')) is not int):
            raise GatewayResultUnknown('borrowed gateway execution response is invalid')
        if result['code'] not in (0, 1):
            raise GatewayRejected('borrowed gateway rejected scene execution')
        return True


class XiaomiHomeGateway:
    def __init__(self, hass, uid, region):
        self.hass = hass
        self.uid = _id(uid)
        self.region = region
        self._compatible = False
        self._closed = False

    def _match(self, owner, home):
        clients = self.hass.data.get('xiaomi_home', {}).get('miot_clients', {})
        group = home_group_id(owner, home)
        matches = []
        for entry in self.hass.config_entries.async_entries('xiaomi_home'):
            data = entry.data
            if (entry.state != ConfigEntryState.LOADED or str(data.get('uid')) != self.uid
                    or data.get('cloud_server') != self.region):
                continue
            homes = data.get('home_selected')
            selected = homes.get(home) if isinstance(homes, dict) else None
            if (not isinstance(selected, dict) or selected.get('group_id') != group
                    or str(selected.get('uid')) != owner):
                continue
            client = clients.get(entry.entry_id)
            locals_ = getattr(client, '_mips_local', {})
            local = locals_.get(group) if isinstance(locals_, dict) else None
            if (local and _session(local) is not None
                    and callable(getattr(local, '_MipsLocalClient__request_external', None))
                    and all(callable(getattr(local, name, None)) for name in (
                        'sub_mips_state', 'unsub_mips_state'))
                    and all(callable(getattr(client, name, None)) for name in (
                        'sub_manual_scene_state', 'unsub_manual_scene_state'))):
                matches.append((entry, client, local, group))
        return matches[0] if len(matches) == 1 else None

    async def qualify(self, home, timeout=15):
        if self._closed:
            raise GatewayUnavailable('borrowed gateway is closed')
        try:
            async with asyncio.timeout(timeout):
                integration = await async_get_integration(self.hass, 'xiaomi_home')
                if str(integration.version) != VERIFIED_VERSION or not await self.hass.async_add_executor_job(
                        _fingerprint, integration.file_path):
                    self._compatible = False
                    raise GatewayUnavailable('unverified Xiaomi Home gateway implementation')
        except (IntegrationNotFound, ImportError, OSError, TimeoutError) as exc:
            self._compatible = False
            raise GatewayUnavailable('Xiaomi Home compatibility cannot be verified') from exc
        self._compatible = True
        scope = _id(home.get('owner_uid')), _id(home.get('home_id'))
        match = self._match(*scope)
        if not match:
            raise GatewayUnavailable('no unique matching live Xiaomi Home gateway')
        entry, client, local, group = match
        route = getattr(client, '_scene_routes', {}).get(group)
        groups = getattr(route, 'scene_ids', None)
        if (getattr(route, 'client', None) is not local or not isinstance(groups, frozenset)
                or any(type(value) is not str or not value.strip() for value in groups)):
            raise GatewayUnavailable('Xiaomi Home has no confirmed scene qualification')
        return HomeSceneSnapshot(self, entry, client, local, group, route, scope,
                                 _session(local), groups, self.hass.loop.time() + 600)

    async def _request(self, local, generation, topic, payload, timeout=10):
        future = self.hass.loop.create_future()

        def complete(value):
            if not future.done():
                future.set_result(value)

        def received(value, context):
            self.hass.loop.call_soon_threadsafe(complete, value)

        try:
            submitted = local._MipsLocalClient__request_external(
                topic, json.dumps(payload, separators=(',', ':')), received,
                on_reply_ctx=future, timeout_ms=int(timeout * 1000), generation=generation,
            )
            if submitted is not True:
                raise GatewayResultUnknown('borrowed gateway submission outcome unknown')
            reply = json.loads(await asyncio.wait_for(future, timeout))
            if (not isinstance(reply, dict) or 'error' in reply
                    or ('code' in reply and (type(reply['code']) is not int or reply['code'] != 0))):
                raise GatewayResultUnknown('borrowed gateway response is invalid')
            return reply
        except (OSError, TimeoutError, ValueError, TypeError) as exc:
            raise GatewayResultUnknown('borrowed gateway result unknown') from exc
        finally:
            if not future.done():
                future.cancel()

    async def close(self):
        # Only revoke this adapter. Never close or change the borrowed client.
        self._closed = True
        self._compatible = False
