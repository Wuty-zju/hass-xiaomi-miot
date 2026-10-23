# Local central-gateway manual scenes

Xiaomi Miot can run a manual scene through a central gateway on the same LAN.
This feature is **off by default**. A Xiaomi Miot cloud account is still needed
to discover manual scene buttons. In the original **cloud options** form,
set the manual-scene gateway source to one of:

- **Off:** all manual scene buttons use Miot's cloud scene API, regardless of
  the device connection mode. This does not delete any previously saved grant.
- **Reuse Xiaomi Home:** requires a verified Xiaomi Home implementation to be
  installed, configured for the same account and region, and running with a
  selected home and live local gateway. Miot uses its existing in-memory scene
  eligibility and generation-fenced queue; it never reads Xiaomi Home's OAuth
  token, certificate, or storage, and never owns or closes its connection.
  These methods are not a stable cross-integration API. An incompatible Xiaomi
  Home version or unloaded gateway causes a safe local-unavailable result.
  Currently the reviewed Wuty-zju implementation at commit
  `d740f351022a0ec138b00d0ab864a2278377268f` / `v0.6.4.3` is supported only
  when both source fingerprints and runtime capabilities match. Version numbers
  alone are insufficient; official builds without this contract fail closed.
- **Independent authorization:** Miot maintains its own OAuth grant, virtual
  device ID, client certificate, mDNS discovery, and MQTT connection. Existing
  credentials can be kept or renewed. The grant and private key are stored in
  Home Assistant's private `.storage`; a temporary key file is removed on
  unload. Protect Home Assistant backups accordingly.

Independent authorization uses Xiaomi's registered
`http://homeassistant.local:8123` redirect. If a remote browser or reverse
proxy cannot reach that address, copy the **complete callback URL** from the
browser address bar into the authorization form. Miot validates its webhook
path and OAuth state before exchanging the code. The callback URL contains a
one-time secret: never share it or paste it into issue reports. Xiaomi can
return JSON with a `text/plain` media type; Miot parses that response without
logging the code or request URL. An API error code still means Xiaomi rejected
the grant; a successful HTTP status alone is not proof of authorization.

When a local gateway source is enabled, the existing account connection mode
governs manual scene buttons:

| Mode | Scene button behavior |
| --- | --- |
| `auto` | Prefer a matching LAN gateway. Fall back to `NewRunScene` only before a scene-execution command may have been sent. |
| `local` | Require a matching, authorized LAN gateway; never call the cloud scene API. |
| `cloud` | Use the existing cloud scene API without contacting a gateway. |

An omitted connection mode keeps the original cloud-only default. Disabling
manual scenes creates no gateway work. Reuse is an explicit temporary or
long-term choice, never a hidden native → borrowed → cloud retry chain.

Metadata loads from the account cache while cloud refresh runs in the background.
Failed home reads preserve matching metadata; a confirmed complete empty list
removes active buttons but preserves their registry records. Background home
workers acquire current gateway eligibility, retry with bounded backoff, and
refresh before expiry. Button properties and activation use the same memory
route; activation does not query lists, discover gateways or renew certificates.
Existing account setup/authentication is still required: the metadata cache is
not a separate offline account bootstrap mechanism.

The independent transport matches the scene owner and home group against
`_miot-central._tcp.local.` advertisements. Reuse mode additionally checks
the Xiaomi Home account, region, selected home, live local connection, and
available scene methods. Both check the scene ID before dispatch. A missing
gateway or scene is safe for `auto` cloud fallback. Once a scene-execution
request may have been sent, a timeout is **not** retried through the cloud:
the scene could otherwise run twice. A gateway reply with code `0` or `1`
confirms acceptance, not that every downstream action finished offline.

Eligibility is never restored from disk. It is bound to owner/home, current
gateway/connection generation and expiry. Disconnects or discovery changes
revoke the native route and wake background refresh. Unload/stop cancels owned
work and subscriptions; it cannot retract an already published action. Per-scene
overlap protection is in-process, not cross-App exactly-once execution.

To support a changed Xiaomi Home version, first review the source's reply-ready
generation, queued publication checks, timeout/cancellation and subscription
ownership, then update compatibility fingerprints and regression tests. Do not
replace the guard with a broad version range or method-name check. No Xiaomi
Home source is imported or copied by the adapter; the private API remains an
explicit maintenance risk rather than a supported upstream interoperability API.

The independent local protocol is based on observable gateway behavior and
Xiaomi's published Home Assistant integration. Its separately licensed Xiaomi
CA certificates and attribution are documented in
`custom_components/xiaomi_miot/LICENSES/`. Xiaomi's license limits use of its
licensed work to non-commercial Home Assistant use; this fork does not grant
additional rights or claim Xiaomi endorsement.
