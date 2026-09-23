# Local central-gateway manual scenes

Xiaomi Miot can run a manual scene directly through a central gateway on the
same LAN. This is independent of the Xiaomi Home integration: Miot maintains
its own OAuth grant, virtual device ID, client certificate, mDNS discovery,
and MQTT connection. A Xiaomi account remains necessary for discovering the
manual scene buttons and for initial OAuth/certificate issuance.

After adding a Xiaomi cloud account, open the integration's options and choose
**Authorize local central gateway**. Complete the Xiaomi authorization from a
browser that can reach `homeassistant.local:8123`; the OAuth callback must
reach the Home Assistant webhook. The grant and private key are stored in
Home Assistant's private `.storage`, and a temporary key file is removed when
the integration unloads. Protect Home Assistant backups accordingly.

The account's connection mode also governs its manual scene buttons:

| Mode | Scene button behavior |
| --- | --- |
| `auto` | Prefer a matching LAN gateway. Fall back to `NewRunScene` only before a scene-execution command may have been sent. |
| `local` | Require a matching, authorized LAN gateway; never call the cloud scene API. |
| `cloud` | Use the existing cloud scene API without contacting a gateway. |

The button derives the home group ID from the scene's owner UID and home ID,
matches that exact ID against `_miot-central._tcp.local.` advertisements,
and checks that the scene ID appears in the gateway's action-group list before
sending `master/proxy/execMijiaActionGroup`. A missing gateway or missing
scene is safe for `auto` cloud fallback. Once a scene-execution MQTT publish
may have occurred, a timeout is **not** retried through the cloud; the result
is unknown and a retry could trigger the scene twice. A gateway reply with
code `0` or `1` means the request was accepted, not that every downstream
device action has been proven to finish offline.

The local protocol is based on observable gateway behavior and Xiaomi's
public Home Assistant implementation. The separately licensed Xiaomi CA
certificates bundled for TLS verification are documented in
`custom_components/xiaomi_miot/LICENSES/`.
