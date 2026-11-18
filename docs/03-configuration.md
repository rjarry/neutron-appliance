<sub>[Index](intro.md) · [&larr; Hooking into Neutron](02-hooking-neutron.md) · [First draft &rarr;](04-first-draft.md)</sub>

# 3. Configuration and state

## 3.1 The document, and the generation number

One JSON document per router, the single source of truth:

```json
{
  "schema_version": 1,
  "router_id": "7f0e...",
  "generation": 42,
  "admin_state_up": true,
  "interfaces": [
    {"mac": "fa:16:3e:11:22:33", "role": "internal",
     "addresses": ["10.0.0.1/24"], "mtu": 1442},
    {"mac": "fa:16:3e:44:55:66", "role": "external",
     "addresses": ["172.24.4.9/24"], "mtu": 1500}
  ],
  "routes": [
    {"destination": "0.0.0.0/0",      "nexthop": "172.24.4.1"},
    {"destination": "192.168.0.0/24", "nexthop": "10.0.0.5"}
  ],
  "ipsec": []
}
```

Three rules, all load-bearing:

1. **Interfaces are identified by MAC, never by kernel device name.** Astara
   and Octavia converged on this independently.
2. **It is complete desired state, not a delta.** Applying it twice is a no-op;
   applying it to a freshly rebooted VM is the normal path, not recovery.
3. **The agent knows nothing about Neutron** — interfaces, routes and tunnels,
   no Neutron semantics. Astara got this right and it made its appliance
   reusable.

`schema_version` exists because of Octavia's hardest lesson: the control plane
stays compatible with in-field agents indefinitely. Add fields, never repurpose
them.

**`generation`** is a monotonic integer per router, bumped by the driver in the
same transaction as the change. It is the whole synchronisation mechanism:
`applied == desired` is converged, `applied < desired` is converging, no answer
is unknown. No locks, no negotiation. An agent ignores any document whose
generation is not greater than what it has applied, which makes a stale read
harmless.

## 3.2 Persisted state

It must not live in process memory. The PoC keeps the router-to-VM map and
health state in whichever process handled the request, with a Nova
naming-convention `_discover_vm()` papering over `neutron-api` and
`neutron-rpc-server` being different processes.

| Stored | Why |
|---|---|
| router_id to instance_id | The endpoint maps instance back to router; the reconciler needs it to spot orphans |
| `generation` | §3.1 |
| The rendered document, or enough to re-render deterministically | The endpoint must answer fast (§3.3) without a multi-table join |
| Lifecycle state and attempt counters | §5 |
| Agent credential fingerprint and issue time | §3.6 |
| The sizing profile snapshot | [§2.8](02-hooking-neutron.md#28-vm-sizing-via-metainfo) |

Deliberately **not** stored: anything readable back from Nova or Neutron at
reconcile time. Cached copies of reality are a liability.

The mechanism is [`Q2`](open-questions.md): an out-of-tree package can own an alembic branch, but
this is where out-of-tree is genuinely harder than in-tree, and it depends on
[`Q1`](open-questions.md) — a separate manager could own its own database, Octavia-style.

## 3.3 Delivery: the agent pulls, over the management network

> **Changed.** Earlier drafts delivered configuration through **Nova dynamic
> vendordata**. That channel was measured and works — see
> [findings.md](findings.md) — but it has been set aside because it couples us
> to `nova.conf` and, in RHOSO, to nova-operator. The measurements remain
> valid and the option remains open; we are simply not taking it.

Two pieces, one of which never changes:

| | Carries | Mechanism | Changes? |
|---|---|---|---|
| **Bootstrap** | router ID, API endpoint, credentials, CA | **config drive**, written at boot | never |
| **Configuration** | the full desired-state document | the agent **pulls** it from a Neutron API endpoint | every change |

The agent therefore talks to exactly one service, directly, with no metadata
service and no relay in between.

### 3.3.1 One request, both directions

The agent reports what it has and receives what it should have, in a single
round trip:

```
POST {endpoint}/v2.0/appliance/routers/{router_id}/sync
X-Auth-Token:      <keystone token, service user>
X-Appliance-Token: <per-router secret>

{"schema_version": 1, "applied_generation": 41,
 "state": "converging", "errors": [], "boot_id": "..."}
```

```
200 OK
{"schema_version": 1, "generation": 42, "poll_interval_s": 30,
 "config": { ... }}
```

- `config` is **omitted when `generation == applied_generation`**, so the
  common case is a small heartbeat.
- The server records `last_seen` on every call. **That is the health signal**;
  there is no separate liveness check.
- Nothing ever connects *to* the appliance. No listening socket, no server
  certificate, no ingress routing, and the management security group needs no
  ingress rules at all.

The normative contract, including credential rotation and the failure
behaviour of every status code, is [protocol.md](protocol.md).

### 3.3.2 The cost, and how it is contained

Vendordata gave **boot independence**: an appliance could reconverge with no
management network and no control plane. Pulling gives that up, so it has to
be bought back in the agent:

> **Hard requirement.** The agent persists the last applied configuration to
> disk and applies it on boot *before* contacting the control plane.
> Forwarding never depends on the control plane being reachable. Only
> *changes* do.

Three consequences that must be implemented and tested, not assumed:

- The appliance boots and forwards with the management network down.
- A sync failure is **never** a reason to tear down configuration.
- On-disk state wins at boot; the server's answer wins afterwards, by
  generation number.

## 3.4 Management network

Required, now, for configuration changes rather than only for observability.
One **shared** network per deployment, in the service project, created
idempotently by the server.

| Property | Value |
|---|---|
| Subnet | Sized for the whole cloud, not for a lab |
| Router | A **default-flavour OVN router** with an external gateway. No recursion: it is OVN's router, not ours |
| Security group | **Egress only.** No ingress rules |
| Reachability | VM to the Neutron public endpoint. **Measured working** |

### 3.4.1 RHOSO

The control plane runs as pods, which is exactly what made a
controller-initiated design painful. Pull-only removes the problem:

- The agent needs to reach **one** endpoint, already exposed for ordinary API
  clients.
- A management network behind an OVN router with an external gateway reaches
  it the same way any tenant instance reaches Keystone — **measured**.
- **No Multus attachments, no reserved controller IPs, no link router with
  SNAT disabled, no host routes in either direction.** Every one of those is
  an Octavia `lb-mgmt-net` requirement that we no longer have.

Two details that are easy to get wrong:

1. **The endpoint URL written to config drive must be one the VM can resolve
   and reach**, which is not necessarily the URL `neutron-server` knows itself
   by. Resolve it from the Keystone catalogue and allow an override for
   deployments that need an internal VIP.
2. **TLS must validate.** Ship the CA in the bootstrap blob rather than
   disabling verification.

Fallback where the public endpoint is unreachable from tenant networks: an
operator-provisioned provider network routed to the internal API VIP,
consumed by ID. Design the configuration for it; do not build it yet.

### 3.4.2 Health states

Three, and they must not be collapsed into two:

| State | Meaning |
|---|---|
| converged | synced, `applied == desired` |
| converging | synced, `applied < desired` |
| unknown | no sync within N intervals — may be perfectly healthy but unreachable |

**`unknown` is not an error.** If the management network is down the appliance
is still forwarding, on its cached configuration. Only a *sustained*
`converging`, or an explicit error from the agent, is a fault.

## 3.5 TLS and secrets

**Yes we need TLS, and the reason is IPsec, not routing.** In stage 0 the
document holds IP addresses — not secret but integrity-critical, since forging
it reroutes a tenant's traffic. In stage 2 it holds **PSKs or private key
material**, which turns the requirement into confidentiality as well.

1. **The sync channel must be HTTPS with a validated CA.** Ship the CA in the
   bootstrap blob rather than disabling verification. Good practice at stage
   0, mandatory at stage 2.
2. **Two secrets, two jobs.** A shared Keystone service user gets the agent
   past keystonemiddleware; a per-router token, checked by our extension,
   decides *which* router's configuration it may read. The service user must
   be restricted by policy to the appliance endpoints only — otherwise one
   compromised appliance yields a credential that can read and write Neutron
   cloud-wide, and the second secret is theatre.
3. **Config drive is write-once**, so it carries only the bootstrap token.
   Everything rotatable arrives in the sync response.
4. **Rotation must tolerate a crash mid-rotation.** If the server issues a new
   token and the agent dies before persisting it, the agent still holds the
   old one. Keep the previous hash with an expiry, accept either, and retire
   the old one once the new one is used. Without this, rotation bricks
   appliances, and it will not show up until the first unlucky reboot.
5. **Who else sees the document.** Only the agent and the Neutron API — a
   genuine advantage of dropping vendordata, which transited
   `nova-api-metadata` and the compute node's `ovn-metadata-agent`. Whether
   PSKs may travel in the document at all is still
   [`Q10`](open-questions.md).

Octavia is the reference for the parts we have not solved: it uses **two CAs
deliberately** and warns against collapsing them, since a server certificate
from an amphora could otherwise impersonate a controller. We avoid that
problem entirely by having no server side in the VM.

The PKI itself — CA ownership, key storage (Octavia keeps them Fernet-encrypted
in its DB), whether to reuse an existing RHOSO PKI — is [`Q11`](open-questions.md).

## 3.6 Observability and operator access

The difference between a demo and something anyone will run.

| Metric the manager must export | Why |
|---|---|
| Routers by lifecycle state | The single most useful number |
| Convergence lag `desired - applied`, histogram | Distinguishes "slow" from "stuck" |
| Routers in `unknown` | A spike means a network problem, not a router problem |
| Reconcile duration and backlog | Capacity signal ([§5.5](05-lifecycle.md#55-scale-and-manager-availability)) |
| Nova call latency and errors by operation | Boot and hotplug are the slow paths |
| Endpoint rate, latency, unknown-instance ratio | Early warning for §3.3 |
| Service project quota headroom | [§2.5](02-hooking-neutron.md#25-service-project-quota-and-the-cost-model) |

**Getting logs out of the appliance** is unsolved here and a known problem in
Octavia, which ships amphora logs to the controller over rsyslog precisely
because operators could not debug otherwise. The VM is in a service project and
the tenant cannot see it, so without an answer every failure is diagnosed by
guesswork ([`Q17`](open-questions.md)). The status report's `errors[]` is the cheap half and should
carry real messages.

**Operator access.** Octavia ships a debug-only SSH key, disabled by default;
we want the same default. An appliance that can be logged into is one whose
state changes out of band, which breaks the reconcile model — so if it exists
it is for reading, and the agent overwrites manual changes on its next pass.

**Time synchronisation** is required, not optional: certificate validity and
IKE both depend on it. chrony in the image. Cheap to forget, annoying to debug.
