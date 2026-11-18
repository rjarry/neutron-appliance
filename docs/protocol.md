<sub>[Index](intro.md) · [Configuration](03-configuration.md) · [Open questions](open-questions.md)</sub>

# The agent protocol

Normative reference for the contract between the server and the in-VM agent.
Implemented in `neutron_appliance/common/`, consumed by both halves.

**Every byte here is a multi-release commitment.** A new server must keep
working with an agent baked into an image built months earlier. Add fields,
never repurpose them, and bump `schema_version` when the meaning of an
existing field changes.

Both halves parse the same way: **unknown fields are ignored**, so that
adding one is never a breaking change, while an **unknown `schema_version`
is refused outright**. A field that is present but `null` is treated as
absent. Both ends must therefore be able to express a complete message using
only the fields below.

## 1. Bootstrap

Written once by the server into the instance's config drive, read by
`agent/bootstrap.py`. Only values that never change belong here.

```json
{
  "schema_version": 1,
  "router_id": "7f0e2c3a-...",
  "endpoint": "https://neutron.example.com:9696",
  "sync_path": "/v2.0/appliance/routers/7f0e2c3a-.../sync",
  "ca_cert": "-----BEGIN CERTIFICATE-----\n...",
  "poll_interval_s": 30,
  "credential": {
    "auth_url": "https://keystone.example.com:5000/v3",
    "username": "neutron-appliance",
    "password": "<service user secret>",
    "project_name": "service",
    "user_domain_name": "Default",
    "project_domain_name": "Default",
    "router_token": "<per-router secret>"
  }
}
```

`ca_cert` is omitted when the endpoint presents a certificate signed by a
well-known CA. Both domain names default to `Default`. Everything else is
mandatory.

## 2. Authentication

Two secrets with two distinct jobs:

| Secret | Purpose | Scope |
|---|---|---|
| Keystone service user | Gets the request past keystonemiddleware | Shared by every appliance |
| `router_token` | Decides *which* router's configuration may be read | One router |

```
X-Auth-Token:       <keystone token, obtained as the service user>
X-Appliance-Token:  <router_token>
```

Because the agent presents an ordinary Keystone token, the request traverses
the normal API pipeline: **no `api-paste.ini` change and no exempted path**.

Four rules, each closing a specific hole:

1. **The service user must be able to do nothing else.** A dedicated
   `appliance-agent` role, restricted by policy to the appliance endpoints.
   Its secret is on every config drive, so compromising one appliance exposes
   it; this rule is what bounds the damage.
2. **The router token is the real authorisation.** Bind it to the `router_id`
   in the URL, compare in constant time, store only a SHA-256 hash, never log
   it.
3. **Rotation tolerates a crash mid-rotation.** Keep
   `previous_credential_hash` with an expiry, accept either, retire the old
   one once the new one is used.
4. **The agent persists a new token atomically** — temporary file, then
   rename — before using it.

## 3. Sync

One request does both jobs: report state, receive desired state.

```
POST {endpoint}{sync_path}
Content-Type: application/json

{
  "schema_version": 1,
  "agent_version": "0.1.0",
  "boot_id": "e3b0c442-...",
  "uptime_s": 1234,
  "applied_generation": 41,
  "state": "converged" | "converging" | "error",
  "errors": ["interface fa:16:3e:11:22:33 not present after 90s"],
  "observed": {
    "interfaces": [{"mac": "fa:16:3e:11:22:33", "name": "eth1", "up": true}]
  }
}
```

```
200 OK
{
  "schema_version": 1,
  "generation": 42,
  "poll_interval_s": 30,
  "config": { ... },
  "credential": {"router_token": "<new secret>"}
}
```

Rules:

- **`config` is omitted when `generation == applied_generation`.** The common
  case is a small heartbeat.
- `config` is always **complete desired state**, never a delta.
- `credential` appears only when rotating.
- The server records `last_seen` on every call. **That is the health signal.**
- `boot_id` lets the server detect a reboot or rebuild without guessing.
- The call is idempotent and safe to retry.

## 4. The configuration document

```json
{
  "schema_version": 1,
  "router_id": "7f0e2c3a-...",
  "generation": 42,
  "admin_state_up": true,
  "interfaces": [
    {"mac": "fa:16:3e:11:22:33", "role": "internal",
     "addresses": ["10.0.0.1/24"], "mtu": 1442}
  ],
  "routes": [{"destination": "0.0.0.0/0", "nexthop": "172.24.4.1"}]
}
```

`role` is one of `management`, `internal` or `external`: what the agent has
to do with the interface, in its own terms. `mtu` is optional and left alone
when absent.

Three rules, all load-bearing:

1. **Interfaces are identified by MAC address, never by kernel device name.**
   Astara and Octavia converged on this independently.
2. **Complete desired state, not a delta.** Applying it twice is a no-op;
   applying it to a freshly rebooted VM is the normal path, not recovery.
3. **The agent knows nothing about Neutron.** Interfaces, routes and tunnels —
   no Neutron semantics, no object IDs it has to interpret.

For stage pre-0, `interfaces` and `routes` are empty and the backend applies
nothing.

## 5. Agent behaviour

| Situation | Behaviour |
|---|---|
| Startup | Read config drive, load cached config from disk, **apply it**, then start syncing |
| `200` with a new `generation` | Apply, then report the new `applied_generation` on the *next* call. Never report a generation that is not fully applied |
| Partial failure | Keep the old `applied_generation`, set `state=error`, put a human-readable message in `errors[]` |
| Network failure, `5xx` | Exponential backoff with jitter, capped at 5 minutes. Keep applying cached config |
| `401` / `403` | Back off slowly, log loudly to console as well, re-read the config drive. **Do not discard configuration** |
| `404` | The server does not know this router. Log loudly, keep running, **do not self-destruct** |
| Clock skew, TLS failure | As for network failure, but name the cause in the message |

`errors[]` carries human-readable strings, not codes: *"interface
fa:16:3e:... absent after 90s"*, not `E_NO_IFACE`. Until logs can be shipped
off the appliance, this is the only structured diagnostic channel out of the
VM.
