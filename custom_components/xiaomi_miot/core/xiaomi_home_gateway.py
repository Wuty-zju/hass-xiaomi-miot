"""Optional adapter to an already configured Xiaomi Home local gateway.

Xiaomi Home exposes no supported cross-integration scene API. Keep all access
to its in-memory client in this one module and never read its OAuth storage.
"""

from .gateway_discovery import home_group_id
from .local_gateway import GatewayResultUnknown, GatewayUnavailable


def _local_scene_client(client, group_id):
    locals_by_group = getattr(client, '_mips_local', None)
    local = locals_by_group.get(group_id) if isinstance(locals_by_group, dict) else None
    if (local and getattr(local, 'mips_state', False)
            and callable(getattr(local, 'get_action_group_list_async', None))
            and callable(getattr(local, 'exec_action_group_list_async', None))):
        return local
    return None


def compatible_gateway(hass, uid: str, region: str, home_id: str | None = None,
                       owner_uid: str | None = None):
    """Find a running Xiaomi Home client for exactly this account and home."""
    entries = hass.config_entries.async_entries('xiaomi_home')
    if not entries:
        return None, 'not_configured'
    clients = hass.data.get('xiaomi_home', {}).get('miot_clients', {})
    matching_account = False
    for entry in entries:
        data = entry.data
        if str(data.get('uid')) != str(uid) or data.get('cloud_server') != region:
            continue
        matching_account = True
        homes = data.get('home_selected') or {}
        if not isinstance(homes, dict):
            continue
        client = clients.get(entry.entry_id)
        if not client:
            continue
        if home_id is None:
            if any(_local_scene_client(client, info.get('group_id'))
                   for info in homes.values() if isinstance(info, dict)):
                return client, None
            continue
        selected = homes.get(str(home_id))
        group_id = selected.get('group_id') if isinstance(selected, dict) else None
        if group_id != home_group_id(str(owner_uid or uid), str(home_id)):
            continue
        if _local_scene_client(client, group_id):
            return (client, group_id), None
    return None, 'unavailable' if matching_account else 'account_mismatch'


class XiaomiHomeGateway:
    """Use Xiaomi Home's live transport, never its tokens or certificate."""

    def __init__(self, hass, uid: str, region: str):
        self.hass = hass
        self.uid = str(uid)
        self.region = region

    async def close(self):
        """The official integration owns its clients and their lifecycle."""

    async def run_scene(self, scene) -> bool:
        match, reason = compatible_gateway(
            self.hass, self.uid, self.region, str(scene['home_id']),
            str(scene['owner_uid']),
        )
        if not match:
            raise GatewayUnavailable(f'Xiaomi Home gateway {reason}')
        client, group_id = match
        local = _local_scene_client(client, group_id)
        try:
            groups = await local.get_action_group_list_async()
        except Exception as exc:
            raise GatewayUnavailable('Xiaomi Home scene list unavailable') from exc
        scene_id = str(scene['scene_id'])
        if scene_id not in groups:
            raise GatewayUnavailable('scene not present on Xiaomi Home gateway')
        try:
            result = await local.exec_action_group_list_async(scene_id)
        except Exception as exc:
            raise GatewayResultUnknown('Xiaomi Home scene result unknown') from exc
        if isinstance(result, dict) and result.get('code') in (0, 1):
            return True
        raise GatewayResultUnknown('Xiaomi Home scene was not confirmed')
