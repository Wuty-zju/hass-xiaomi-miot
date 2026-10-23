"""Small shared scene-gateway contracts, without an MQTT or OAuth dependency."""

import hashlib


class GatewayUnavailable(Exception):
    """Execution was not submitted to a transport."""


class GatewayResultUnknown(Exception):
    """A submitted request has no trustworthy completion result; never replay."""


class GatewayRejected(GatewayResultUnknown):
    """An explicitly rejected dispatch must not be replayed either."""


def home_group_id(owner_uid, home_id):
    return hashlib.sha1(f'{owner_uid}central_service{home_id}'.encode()).hexdigest()[:16]


def scene_gateway_enabled(entry):
    """Missing connection mode retains Miot's default cloud-only behavior."""
    return (entry.get_config('conn_mode', 'cloud') in ('auto', 'local')
            and entry.get_config('scene_gateway_mode', 'off') in ('reuse', 'independent')
            and not entry.get_config('disable_manual_scenes', False))
