# Local central-gateway manual scenes

Xiaomi Miot can run a manual scene through a central gateway on the same LAN.
This feature is **off by default**. A Xiaomi Miot cloud account is still needed
to discover manual scene buttons. In the integration options, open **Local
gateway for manual scenes** and select one of:

- **Off:** all manual scene buttons use Miot's cloud scene API, regardless of
  the device connection mode. This does not delete any previously saved grant.
- **Reuse Xiaomi Home:** requires the official Xiaomi Home integration to be
  installed, configured for the same account and region, and running with a
  selected home and live local gateway. Miot uses its in-memory scene-group
  methods; it never reads Xiaomi Home's OAuth token, certificate, or storage.
  These methods are not a stable cross-integration API. An incompatible Xiaomi
  Home version or unloaded gateway causes a safe local-unavailable result.
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

The independent transport matches the scene owner and home group against
`_miot-central._tcp.local.` advertisements. Reuse mode additionally checks
the Xiaomi Home account, region, selected home, live local connection, and
available scene methods. Both check the scene ID before dispatch. A missing
gateway or scene is safe for `auto` cloud fallback. Once a scene-execution
request may have been sent, a timeout is **not** retried through the cloud:
the scene could otherwise run twice. A gateway reply with code `0` or `1`
confirms acceptance, not that every downstream action finished offline.

The independent local protocol is based on observable gateway behavior and
Xiaomi's published Home Assistant integration. Its separately licensed Xiaomi
CA certificates and attribution are documented in
`custom_components/xiaomi_miot/LICENSES/`. Xiaomi's license limits use of its
licensed work to non-commercial Home Assistant use; this fork does not grant
additional rights or claim Xiaomi endorsement.
