"""OAuth and certificate primitives for an independent central-gateway client.

An OAuth client ID and an approved redirect URI must be supplied by the caller.
No Xiaomi Home integration state or password is read by this module.
"""

import base64
import hashlib
import json
import secrets
import time
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlencode, urlparse

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.x509.oid import NameOID


class GatewayAuthorizationError(Exception):
    """A token or gateway certificate could not be obtained."""


# Public Home Assistant OAuth application ID registered by Xiaomi.
OAUTH_CLIENT_ID = '2882303761520251711'
OAUTH_REDIRECT_ORIGIN = 'http://homeassistant.local:8123'


def oauth_host(region: str) -> str:
    if region not in {'cn', 'de', 'i2', 'ru', 'sg', 'us'}:
        raise GatewayAuthorizationError('unsupported Xiaomi cloud region')
    return 'ha.api.io.mi.com' if region == 'cn' else f'{region}.ha.api.io.mi.com'


async def _read_api_response(response, operation: str) -> dict:
    """Xiaomi may return JSON as text/plain, including for API errors."""
    if response.status != 200:
        raise GatewayAuthorizationError(f'{operation} HTTP {response.status}')
    try:
        result = json.loads(await response.text())
    except (ValueError, UnicodeError) as exc:
        status = response.headers.get('X-Xiaomi-Status-Code')
        reason = f'Xiaomi status {status}' if status else 'invalid response'
        raise GatewayAuthorizationError(f'{operation}: {reason}') from exc
    if not isinstance(result, dict) or result.get('code') != 0:
        code = result.get('code') if isinstance(result, dict) else None
        raise GatewayAuthorizationError(f'{operation}: Xiaomi code {code}')
    return result


def authorize_url(client_id: str, redirect_uri: str, device_id: str,
                  state: str | None = None) -> tuple[str, str]:
    """Build an interactive Xiaomi account authorization URL."""
    state = state or secrets.token_urlsafe(24)
    query = urlencode({
        'client_id': client_id,
        'redirect_uri': redirect_uri,
        'response_type': 'code',
        'device_id': f'ha.{device_id}',
        'state': state,
    })
    return f'https://account.xiaomi.com/oauth2/authorize?{query}', state


def callback_authorization_code(url: str, redirect_uri: str, state: str) -> str:
    """Accept a copied redirect URL when the browser cannot reach HA locally."""
    parsed = urlparse(url)
    expected = urlparse(redirect_uri)
    query = parse_qs(parsed.query)
    if (parsed.scheme not in ('http', 'https')
            or parsed.path != expected.path
            or query.get('state') != [state]
            or len(query.get('code', [])) != 1):
        raise GatewayAuthorizationError('gateway callback invalid')
    return query['code'][0]


def generate_certificate_request(uid: str, did: str,
                                 private_key_pem: str | None = None
                                 ) -> tuple[str, str]:
    """Generate a DID-bound Ed25519 CSR, retaining a supplied private key."""
    if private_key_pem is None:
        key = ed25519.Ed25519PrivateKey.generate()
        private_key_pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()
    else:
        key = serialization.load_pem_private_key(private_key_pem.encode(), None)
        if not isinstance(key, ed25519.Ed25519PrivateKey):
            raise GatewayAuthorizationError('gateway key must be Ed25519')
    did_hash = hashlib.sha1(did.encode()).hexdigest()
    name = x509.Name([
        x509.NameAttribute(NameOID.COUNTRY_NAME, 'CN'),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, 'Mijia Device'),
        x509.NameAttribute(NameOID.COMMON_NAME,
                           f'mips.{uid}.{did_hash}.2'),
    ])
    csr = x509.CertificateSigningRequestBuilder().subject_name(name).sign(
        key, algorithm=None,
    )
    return private_key_pem, csr.public_bytes(serialization.Encoding.PEM).decode()


async def exchange_token(session, region: str, client_id: str,
                         redirect_uri: str, device_id: str, *,
                         code: str | None = None,
                         refresh_token: str | None = None) -> dict:
    """Exchange an authorization code, or refresh an existing OAuth token."""
    if (code is None) == (refresh_token is None):
        raise ValueError('exactly one OAuth grant is required')
    grant = {'code': code, 'device_id': f'ha.{device_id}'} if code else {
        'refresh_token': refresh_token,
    }
    payload = {'client_id': int(client_id), 'redirect_uri': redirect_uri, **grant}
    url = f'https://{oauth_host(region)}/app/v2/ha/oauth/get_token'
    async with session.get(url, params={'data': json.dumps(payload)},
                           headers={'content-type': 'application/x-www-form-urlencoded'},
                           timeout=30) as response:
        result = await _read_api_response(response, 'OAuth token request')
    token = result.get('result') if isinstance(result, dict) else None
    if not isinstance(token, dict) or not all(
        token.get(key) for key in ('access_token', 'refresh_token', 'expires_in')
    ):
        raise GatewayAuthorizationError('OAuth token response invalid')
    return {**token, 'expires_at': time.time() + int(token['expires_in']) * 0.7}


async def issue_gateway_certificate(session, region: str, client_id: str,
                                    access_token: str, csr: str) -> str:
    """Issue a client certificate for a previously authorized virtual DID."""
    url = f'https://{oauth_host(region)}/app/v2/ha/oauth/get_central_crt'
    headers = {
        'Authorization': f'Bearer{access_token}',
        'X-Client-AppId': client_id,
        'X-Client-BizId': 'haapi',
    }
    async with session.post(
        url, json={'csr': base64.b64encode(csr.encode()).decode()},
        headers=headers, timeout=30,
    ) as response:
        result = await _read_api_response(response, 'Gateway certificate request')
    certificate = result.get('result') if isinstance(result, dict) else None
    if not isinstance(certificate, dict):
        raise GatewayAuthorizationError('gateway certificate response invalid')
    pem = certificate.get('cert')
    if not isinstance(pem, str) or not pem.startswith('-----BEGIN CERTIFICATE-----'):
        raise GatewayAuthorizationError('gateway certificate missing')
    return pem


async def oauth_account_uid(session, region: str, client_id: str,
                            access_token: str) -> str:
    """Verify the OAuth grant belongs to the configured Miot account."""
    url = f'https://{oauth_host(region)}/app/v2/homeroom/gethome'
    headers = {
        'Authorization': f'Bearer{access_token}',
        'X-Client-AppId': client_id,
        'X-Client-BizId': 'haapi',
    }
    async with session.post(url, json={
        'limit': 150,
        'fetch_share': True,
        'fetch_share_dev': True,
        'plat_form': 0,
        'app_ver': 9,
    }, headers=headers, timeout=30) as response:
        result = await _read_api_response(response, 'OAuth account lookup')
    data = result.get('result') if isinstance(result, dict) else None
    homes = data.get('homelist') if isinstance(data, dict) else None
    if not isinstance(homes, list) or not homes:
        raise GatewayAuthorizationError('OAuth account has no owned home')
    uid = homes[0].get('uid') if isinstance(homes[0], dict) else None
    if uid is None:
        raise GatewayAuthorizationError('OAuth account ID unavailable')
    return str(uid)


def validate_gateway_certificate(pem: str, uid: str, did: str) -> datetime:
    """Reject a certificate issued for another account or virtual device."""
    try:
        certificate = x509.load_pem_x509_certificate(pem.encode())
        common_name = certificate.subject.get_attributes_for_oid(
            NameOID.COMMON_NAME,
        )[0].value
    except (ValueError, IndexError) as exc:
        raise GatewayAuthorizationError('gateway certificate invalid') from exc
    expected = f'mips.{uid}.{hashlib.sha1(did.encode()).hexdigest()}.2'
    if common_name != expected or certificate.not_valid_after_utc <= datetime.now(timezone.utc):
        raise GatewayAuthorizationError('gateway certificate identity invalid')
    return certificate.not_valid_after_utc
