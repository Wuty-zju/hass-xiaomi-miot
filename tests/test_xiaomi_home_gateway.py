"""The optional adapter must not cross account or home boundaries."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.xiaomi_miot.core.gateway_discovery import home_group_id
from custom_components.xiaomi_miot.core.local_gateway import (
    GatewayResultUnknown, GatewayUnavailable,
)
from custom_components.xiaomi_miot.core.xiaomi_home_gateway import (
    XiaomiHomeGateway, compatible_gateway,
)


SCENE = {'owner_uid': '1000', 'home_id': '123', 'scene_id': '42'}
GROUP = home_group_id('1000', '123')


def _hass(uid='1000', region='cn', group=GROUP):
    local = SimpleNamespace(
        mips_state=True,
        get_action_group_list_async=AsyncMock(return_value=['42']),
        exec_action_group_list_async=AsyncMock(return_value={'code': 1}),
    )
    client = SimpleNamespace(_mips_local={GROUP: local})
    entry = SimpleNamespace(entry_id='official', data={
        'uid': uid, 'cloud_server': region,
        'home_selected': {'123': {'group_id': group}},
    })
    hass = SimpleNamespace(
        config_entries=SimpleNamespace(async_entries=lambda domain: [entry]),
        data={'xiaomi_home': {'miot_clients': {'official': client}}},
    )
    return hass, local


def test_reuse_runs_only_matching_home_scene():
    async def run():
        hass, local = _hass()
        assert await XiaomiHomeGateway(hass, '1000', 'cn').run_scene(SCENE)
        local.exec_action_group_list_async.assert_awaited_once_with('42')
        with pytest.raises(GatewayUnavailable):
            await XiaomiHomeGateway(hass, '1000', 'cn').run_scene({
                **SCENE, 'home_id': '456',
            })
        assert local.exec_action_group_list_async.await_count == 1

    asyncio.run(run())


@pytest.mark.parametrize('uid,region,group', [
    ('2000', 'cn', GROUP),
    ('1000', 'us', GROUP),
    ('1000', 'cn', 'wrong-group'),
])
def test_reuse_rejects_other_identity_or_group(uid, region, group):
    hass, _ = _hass(uid, region, group)
    match, reason = compatible_gateway(hass, '1000', 'cn', '123')
    assert match is None
    assert reason


def test_reuse_does_not_cloud_retry_after_unknown_result():
    async def run():
        hass, local = _hass()
        local.exec_action_group_list_async.side_effect = TimeoutError()
        with pytest.raises(GatewayResultUnknown):
            await XiaomiHomeGateway(hass, '1000', 'cn').run_scene(SCENE)
        assert local.exec_action_group_list_async.await_count == 1

    asyncio.run(run())


def test_reuse_requires_live_official_scene_api():
    hass, local = _hass()
    local.mips_state = False
    match, reason = compatible_gateway(hass, '1000', 'cn')
    assert match is None
    assert reason == 'unavailable'


def test_shared_home_uses_owner_group_but_same_authorized_account():
    async def run():
        group = home_group_id('2000', '123')
        hass, local = _hass(group=group)
        hass.data['xiaomi_home']['miot_clients']['official']._mips_local = {
            group: local,
        }
        assert await XiaomiHomeGateway(hass, '1000', 'cn').run_scene({
            **SCENE, 'owner_uid': '2000',
        })
        local.exec_action_group_list_async.assert_awaited_once_with('42')

    asyncio.run(run())
