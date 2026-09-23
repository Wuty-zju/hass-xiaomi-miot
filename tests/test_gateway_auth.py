"""Offline checks for independent gateway credentials."""

import hashlib
import asyncio
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

from cryptography import x509
from cryptography.x509.oid import NameOID

from custom_components.xiaomi_miot.core import gateway_auth
from custom_components.xiaomi_miot.core.gateway_auth import (
    GatewayAuthorizationError,
    authorize_url,
    exchange_token,
    generate_certificate_request,
    issue_gateway_certificate,
    oauth_account_uid,
)
import pytest


def test_authorize_url_has_unique_state_and_no_password():
    url, state = authorize_url('123', 'http://homeassistant.local:8123/hook',
                               'virtual-id')
    params = parse_qs(urlparse(url).query)

    assert params['state'] == [state]
    assert params['client_id'] == ['123']
    assert params['device_id'] == ['ha.virtual-id']
    assert 'password' not in params


def test_certificate_request_binds_uid_and_did():
    key, csr_pem = generate_certificate_request('1000', 'virtual-id')
    csr = x509.load_pem_x509_csr(csr_pem.encode())
    common_name = csr.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value

    assert csr.is_signature_valid
    assert common_name.startswith('mips.1000.')
    assert common_name.endswith('.2')
    assert generate_certificate_request('1000', 'virtual-id', key)[0] == key


def test_bundled_gateway_ca_is_pinned():
    pem = Path(gateway_auth.__file__).with_name('mijia_gateway_ca.pem').read_bytes()
    assert hashlib.sha256(pem).hexdigest() == (
        '8b7bf306be3632e08b0ead308249e5f2b2520dc921ad143872d5fcc7c68d6759'
    )


def test_oauth_token_exchange_rejects_malformed_result():
    class Response:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def json(self):
            return {'code': 0, 'result': {}}

    session = SimpleNamespace(get=lambda *args, **kwargs: Response())
    with pytest.raises(GatewayAuthorizationError):
        asyncio.run(exchange_token(
            session, 'cn', '123', 'http://homeassistant.local:8123/hook',
            'virtual-id', code='code',
        ))


def test_certificate_endpoint_uses_bearer_grant():
    calls = []

    class Response:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def json(self):
            return {'code': 0, 'result': {
                'cert': '-----BEGIN CERTIFICATE-----\nTEST',
            }}

    def post(url, **kwargs):
        calls.append((url, kwargs))
        return Response()

    certificate = asyncio.run(issue_gateway_certificate(
        SimpleNamespace(post=post), 'cn', '123', 'token', 'csr',
    ))
    assert certificate.startswith('-----BEGIN CERTIFICATE-----')
    assert calls[0][1]['headers']['Authorization'] == 'Bearertoken'


@pytest.mark.parametrize('payload', [None, {'code': 0, 'result': None},
                                           {'code': 0, 'result': {'homelist': [None]}}])
def test_account_lookup_rejects_malformed_response(payload):
    class Response:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def json(self):
            return payload

    session = SimpleNamespace(post=lambda *args, **kwargs: Response())
    with pytest.raises(GatewayAuthorizationError):
        asyncio.run(oauth_account_uid(session, 'cn', '123', 'token'))
