"""Offline validation of central-gateway mDNS advertisements."""

import base64
from types import SimpleNamespace

from custom_components.xiaomi_miot.core.gateway_discovery import (
    GatewayAddress,
    home_group_id,
    parse_gateway_service,
)


def _advertisement(role=1, mqtt=True):
    profile = bytearray(23)
    profile[1:9] = (123456).to_bytes(8, 'big')
    profile[9:17] = bytes.fromhex('0807060504030201')
    profile[20] = role << 4
    profile[22] = 2 if mqtt else 0
    return SimpleNamespace(
        properties={b'profile': base64.b64encode(profile)},
        parsed_addresses=lambda version: ['192.0.2.10'],
        port=8883,
    )


def test_parse_gateway_service():
    assert parse_gateway_service(_advertisement()) == GatewayAddress(
        did='123456', group_id='0102030405060708',
        host='192.0.2.10', port=8883,
    )


def test_home_group_id_is_deterministic():
    assert home_group_id('1000', '123') == '2a47780246efeac8'


def test_reject_non_central_or_non_mqtt_service():
    assert parse_gateway_service(_advertisement(role=2)) is None
    assert parse_gateway_service(_advertisement(mqtt=False)) is None


def test_reject_invalid_profile():
    advertisement = _advertisement()
    advertisement.properties[b'profile'] = b'not base64%'
    assert parse_gateway_service(advertisement) is None
