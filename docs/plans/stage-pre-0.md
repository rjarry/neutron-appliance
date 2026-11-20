# Stage pre-0: implementation plan

The current plan for this repository. The design it implements is in
[`../`](../); the devstack lab and the eight experiments behind
[`../findings.md`](../findings.md) live in the separate `neutron-stf-poc`
repository.

Read in this order:

1. §1 and §2 — scope, and the decisions that supersede the design documents.
2. The design chapters, starting at [`../intro.md`](../intro.md).
3. §3 onwards — layout, protocol, milestones.

> **Do not implement the vendordata channel described in
> `docs/03-configuration.md` §3.3.** It is measured, it works, and it has been
> set aside. See §2.

---

## 1. Scope

Stage pre-0 is **scaffolding plus one end-to-end loop with no data plane**. It
exists to prove the plumbing before any packet is forwarded.

**Definition of done.** On a devstack ML2/OVN deployment:

1. `openstack router create --flavor-id <appliance> r1` returns immediately.
2. A VM appears in the service project within a bounded time.
3. The VM boots, reads its bootstrap data, reaches the control plane, and
   performs its first sync.
4. The sync reports `applied_generation = 1` for an **empty** configuration.
5. `openstack router show r1` carries `appliance:state=converged`, and the
   status API agrees.
6. `openstack router delete r1` removes the VM, the ports and the DB row,
   leaving no orphans.
7. Stopping the control plane does not disturb the running VM; the agent
   backs off, keeps its cached configuration, and recovers when it returns.

**Explicitly out of scope.** Forwarding of any kind, addresses on data
interfaces, SNAT, floating IPs, extra routes, IPsec, HA, IPv6, trunk ports,
SR-IOV, performance work. The agent has a backend abstraction with exactly one
implementation that **logs what it would do and applies nothing**.

Stage 0 — static IPv4 routing on the Linux stack — is
[`../04-first-draft.md`](../04-first-draft.md) and comes next.

---

## 2. Decisions that supersede the design documents

These come from the project owner and **override** the corresponding sections.
Apply them when copying the documentation (§10).

| Topic | Documents say | **Now** | Rationale |
|---|---|---|---|
| Config transport | Nova dynamic vendordata (§3.3) | **Config drive carries bootstrap only. The agent pulls live config directly from a Neutron API endpoint** | Removes all `nova.conf` coupling and the nova-operator dependency. Closes `Q9`. One hop instead of three |
| Status channel | Separate channel, direction undecided (`Q4`) | **Merged into the same request** — the agent reports status and receives config in one round trip | One call, one credential, one failure mode. Closes `Q4` |
| Direction | Controller-to-guest poll was the previous model | **Guest to control plane only** | No inbound path to the appliance at all. No listening socket, no server certificate, no ingress routing |
| Where slow work runs (`Q1`) | Recommended a separate service | **`PeriodicWorker` inside `neutron-server`** | Fewer moving parts now; revisit if it hurts. Closes `Q1` provisionally |
| Management network | Needed for status only | **Needed for configuration changes** | Accepted trade-off, mitigated below |
| SR-IOV | Possible, untested (§2.7) | **Out of scope**, but do not foreclose it | Keep the backend and port-creation code free of virtio assumptions |

### 2.1 The one regression, and how to contain it

Vendordata gave **boot independence**: an appliance could reconverge with no
management network and no control plane. Pulling from the Neutron API gives
that up, so it must be bought back in the agent:

> **Hard requirement.** The agent persists the last applied configuration to
> disk and applies it on boot *before* contacting the control plane.
> Forwarding never depends on the control plane being reachable. Only
> *changes* do.

Consequences to implement and test:

- The appliance must boot and become functional with the management network
  down. Test this explicitly (acceptance item 7).
- A sync failure is **never** a reason to tear down configuration.
- On-disk state is the source of truth at boot; the server's answer wins
  afterwards, by generation number.

### 2.2 What is unchanged

Everything else in the design documents still holds: one router per VM, the
service project, Neutron ports as pure IPAM records with our own ipless ports
hot-attached ([`../02-hooking-neutron.md`](../02-hooking-neutron.md)
§2.7 and §2.9), interfaces identified by MAC, full-state idempotent documents,
monotonic generation numbers, level-triggered reconciliation.

---

## 3. Repository layout

Top-level Python package: **`neutron_appliance`**.

```
neutron-appliance/
├── neutron_appliance/
│   ├── common/                 # shared by server and agent. NO neutron imports
│   │   ├── schema.py           # versioned config document + sync models
│   │   ├── protocol.py         # paths, content types, state enum, errors
│   │   └── version.py          # SCHEMA_VERSION, AGENT_API_VERSION
│   ├── server/                 # runs inside neutron-server
│   │   ├── driver.py           # L3ServiceProvider subclass
│   │   ├── config.py           # oslo.config options, [appliance] group
│   │   ├── api/
│   │   │   └── extensions/
│   │   │       └── appliance.py  # sync + status endpoints
│   │   ├── policies.py         # registered policy defaults
│   │   ├── credentials.py      # per-router token generation and hashing
│   │   ├── db/
│   │   │   ├── models.py
│   │   │   ├── api.py
│   │   │   └── migration/alembic_migrations/
│   │   ├── worker.py           # PeriodicWorker reconciler
│   │   ├── reconcile.py        # the converge() logic, no I/O framework
│   │   ├── compute.py          # Nova client wrapper
│   │   ├── networking.py       # mgmt network, ports, service project
│   │   └── render.py           # router state -> config document
│   └── agent/                  # runs inside the appliance VM
│       ├── main.py
│       ├── bootstrap.py        # read config drive
│       ├── client.py           # sync client
│       ├── state.py            # on-disk cache of last applied config
│       ├── console.py          # breadcrumbs to /dev/console
│       └── backends/
│           ├── base.py
│           └── noop.py         # pre-0: log, apply nothing
├── etc/
│   ├── neutron/neutron.conf.d/appliance.conf.sample
│   └── systemd/neutron-appliance-agent.service
├── devstack/plugin.sh
├── image/                      # appliance image build recipe
├── tools/
│   └── neutron-appliance-debug
├── docs/                       # see §10
├── releasenotes/
├── setup.cfg / pyproject.toml
└── tox.ini
```

### 3.1 The package boundary is load-bearing

`neutron_appliance.agent` and `neutron_appliance.common` **must not import
Neutron**. The appliance image must not carry the Neutron server stack.

Enforce it two ways — a `ruff` banned-api rule scoped to those directories
(§12.1), which fails in the editor, and a unit test for transitive imports the
linter cannot see:

```python
def test_agent_imports_without_neutron(self):
    # fails the build if anyone adds a neutron import to the agent
    with blocked_modules('neutron', 'neutron_lib'):
        importlib.import_module('neutron_appliance.agent.main')
        importlib.import_module('neutron_appliance.common.schema')
```

Whether this becomes two separately built distributions is **`Q-C`** in §11.
Keep the boundary clean either way so the split stays cheap.

### 3.2 Entry points

```ini
[entry_points]
console_scripts =
    neutron-appliance-agent = neutron_appliance.agent.main:main
    neutron-appliance-debug = neutron_appliance.tools.debug:main
neutron.db.alembic_migrations =
    neutron-appliance = neutron_appliance.server.db.migration:alembic_migrations
neutron.policies =
    neutron-appliance = neutron_appliance.server.policies:list_rules
oslo.policy.policies =
    neutron-appliance = neutron_appliance.server.policies:list_rules
oslo.config.opts =
    neutron-appliance = neutron_appliance.server.config:list_opts
```

The alembic entry point is the standard mechanism for an out-of-tree Neutron
plugin to own a migration branch, and it resolves `Q2` for the server side. No
stevedore entry point is needed for the driver itself — it is loaded by dotted
path ([`../01-goal.md`](../01-goal.md) §1.1).

`neutron.policies` is what makes the appliance rules exist without an operator
pasting them into `policy.yaml`; `oslo.policy.policies` is the same list, for
`oslopolicy-sample-generator`.

### 3.3 The API extension lives in a directory of its own

Implemented as `server/api/extensions/appliance.py`, not the
`server/api/extension.py` of the layout above. Neutron's extension loader
does not import a module: it scans each directory in `api_extensions_path`,
executes every `*.py` it finds there by path, and looks for a class whose
name is the file's. So the file must be alone in a directory that contains
nothing else it should not execute, and the class name is dictated by the
file name — `appliance.py` must define `Appliance`.

Nothing claims the extension either, because `supported_extension_aliases` is
a plugin's mechanism and we are a service provider behind the L3 plugin. The
module calls `register_custom_supported_check()` at import time, which is
Neutron's supported way of saying so.

---

## 4. The sync protocol

This is the core contract. Specify it in `common/` and version it from day
one; the control plane must stay compatible with in-field agents
indefinitely.

### 4.1 Bootstrap, delivered once at boot

Written by the server into the instance's **config drive** (see `Q-B`),
consumed by `agent/bootstrap.py`:

```json
{
  "schema_version": 1,
  "router_id": "7f0e2c3a-...",
  "endpoint": "https://neutron.example.com:9696",
  "sync_path": "/v2.0/appliance/routers/7f0e2c3a-.../sync",
  "credential": "<opaque bearer token>",
  "ca_cert": "-----BEGIN CERTIFICATE-----\n...",
  "poll_interval_s": 30
}
```

Only values that never change belong here. Anything rotatable arrives through
the sync response instead, because config drive is write-once.

The `credential` block carries both secrets described in §4.5:

```json
"credential": {
  "auth_url": "https://keystone.example.com:5000/v3",
  "username": "neutron-appliance",
  "password": "<service user secret>",
  "project_name": "service",
  "router_token": "<per-router secret>"
}
```

### 4.2b Authentication: two secrets, two jobs

**Decided (`Q-A`): a shared Keystone service user for admission, plus a
per-router token for authorisation.** The agent obtains a Keystone token as
the service user and presents it normally, so the request passes
keystonemiddleware like any other API call — **no `api-paste.ini` change and
no exempted path**, which is the main operational reason for this choice. Our
extension then checks the per-router token.

```
POST /v2.0/appliance/routers/{router_id}/sync
X-Auth-Token:       <keystone token, service user>
X-Appliance-Token:  <per-router secret>
```

Four rules, each closing a specific hole:

1. **The service user must be able to do nothing else.** Define a dedicated
   role (`appliance-agent`) and a policy rule that grants it *only* the
   appliance sync and status endpoints. Otherwise one compromised appliance
   yields a credential that can list and create Neutron resources cloud-wide.
   This is the difference between the two secrets being defence in depth and
   being theatre.
2. **The per-router token is the real authorisation.** It must be bound to the
   `router_id` in the URL and compared in constant time. Store it **hashed**
   (SHA-256 is sufficient for a high-entropy random token; a KDF is not
   needed). Never log it.
3. **Rotation must tolerate a crash mid-rotation.** If the server issues a new
   token and the agent dies before persisting it, the agent still holds the
   old one. So keep `credential_hash` *and* `previous_credential_hash` with an
   expiry, **accept either**, and retire the previous one only once the agent
   has authenticated with the new one. Without this, rotation bricks
   appliances, and it will not show up in testing until the first unlucky
   reboot.
4. **The agent persists a new token atomically** — write to a temporary file
   and rename — before using it.

Acknowledged weakness, to be written down rather than glossed over: the
service user secret is on every appliance's config drive, so compromising one
appliance exposes it. Rule 1 is what bounds the damage, and rule 2 is what
stops the attacker reading another router's configuration.

### 4.2 The sync call

One request, both jobs:

```
POST {endpoint}{sync_path}
Content-Type: application/json
X-Auth-Token:      <keystone token, service user>
X-Appliance-Token: <per-router secret>

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
  "credential": "<new token>"
}
```

Rules:

- **`config` is omitted when `generation == applied_generation`.** The common
  case is a small heartbeat.
- `config` is always **complete desired state**, never a delta.
- `credential` appears only when rotating, and carries the new
  `router_token`. The agent persists it atomically and uses it from the next
  call. This is how rotation works without an inbound channel. The server
  keeps accepting the previous token until the new one is used (§4.2b, rule
  3).
- The server records `last_seen` on every call. **That is the health signal** —
  there is no separate liveness check.
- `boot_id` lets the server detect a reboot or rebuild without guessing.
- The call is idempotent and safe to retry.

### 4.3 Agent behaviour

| Situation | Behaviour |
|---|---|
| Startup | Read config drive, load cached config from disk, apply it, *then* start syncing |
| `200` with new `generation` | Apply, then report the new `applied_generation` on the next call. **Never report a generation that is not fully applied** |
| Partial failure | Keep the old `applied_generation`, set `state=error`, put a human-readable message in `errors[]` |
| Network failure, `5xx` | Exponential backoff with jitter, capped (suggest 5 min). Keep applying cached config |
| `401` / `403` | Back off slowly and log loudly, to console as well. Re-read the config drive in case the bootstrap token is still valid. **Do not discard configuration** |
| `404` | The server does not know this router. Log loudly, keep running, **do not self-destruct** |
| Clock skew, TLS failure | Same as network failure. Make the error message name the cause |

### 4.4 Pre-0 payload

The rendered document for pre-0 is deliberately empty of data-plane content:

```json
{"schema_version": 1, "router_id": "...", "generation": 1,
 "admin_state_up": true, "interfaces": [], "routes": []}
```

---

## 5. Management network

One **shared** management network per deployment (see `Q-G`), in the service
project, created idempotently by the server on first use.

| Property | Value |
|---|---|
| Name | `[appliance] mgmt_network_name`, default `neutron-appliance-mgmt` |
| Subnet | `[appliance] mgmt_subnet_cidr`, default `192.168.240.0/22` — size it for the whole cloud, not for a lab |
| Router | A **default-flavour OVN router** with an external gateway. No recursion: it is OVN's router, not ours |
| Security group | **Egress only.** No ingress rules at all |
| Reachability | VM to the Neutron public endpoint. **Measured working** (experiment 06) |

The empty ingress rule set is worth noticing: because the model is pull-only,
nothing ever connects *to* the appliance, so there is nothing to allow in.
That is a direct security benefit of decision 5.

### 5.1 RHOSO

The control plane runs as pods, which is what made the previous
controller-initiated design painful. Pull-only removes the problem:

- The agent needs to reach **one** endpoint, the Neutron API. In RHOSO that is
  already exposed outside the cluster for ordinary API clients.
- A management network behind an OVN router with an external gateway reaches
  it the same way any tenant instance reaches Keystone — **measured**, VM to
  API worked in the lab with no special plumbing.
- **No Multus attachments, no reserved controller IPs, no link router with
  SNAT disabled, no host routes in either direction.** Every one of those is an
  Octavia `lb-mgmt-net` requirement that we no longer have.

Two things the implementation must get right:

1. **The endpoint URL baked into config drive must be one the VM can resolve
   and reach**, which is not necessarily the URL `neutron-server` knows itself
   by. Resolve it from the Keystone catalogue (`public` interface) and allow
   `[appliance] api_endpoint_override` for deployments that need an internal
   VIP or a dedicated provider network.
2. **TLS must validate.** Ship the CA in the bootstrap blob rather than
   disabling verification. If the public endpoint uses a well-known CA, the
   field is simply empty.

Fallback for deployments where the public endpoint is unreachable from tenant
networks: an operator-provisioned provider network routed to the internal API
VIP, consumed by ID. Design the config for it; do not build it in pre-0.

---

## 6. Router status

There is **no supported way to write a router's `status`** — it is set at
creation and never moves. Do not try. Use three layers instead:

**1. An authoritative read-only API** — part of our extension:

```
GET /v2.0/appliance/routers/{router_id}
{"router_id": "...", "state": "converged", "desired_generation": 42,
 "applied_generation": 42, "last_seen": "2026-11-18T10:03:11Z",
 "instance_id": "...", "errors": []}
```

**2. Router tags, as the operator-visible mirror.** Verified working today:

```
$ openstack router set r1 --tag appliance:state=converged --tag appliance:generation=1
$ openstack router show r1 -f value -c tags
['appliance:generation=1', 'appliance:state=converged']
```

Rewrite only tags with the `appliance:` prefix; never clobber user tags. This
makes `openstack router list --tags appliance:state=error` work, which is the
single most useful operator query. **Caveat to document: tags are
user-writable, so they are a convenience view and not the source of truth.**

**3. Logs and metrics**, per
[`../03-configuration.md`](../03-configuration.md) §3.6.

Three states only, and do not collapse them: `converged`, `converging`,
`unknown` (no sync within N intervals). **`unknown` is not an error** — the
appliance is probably forwarding happily. Closes nothing, but makes `Q8`
concrete.

---

## 7. Debugging a faulty router

Build this in pre-0, not later. The appliance lives in a service project the
tenant cannot see; without these, every failure is diagnosed by guesswork.

| Mechanism | Detail |
|---|---|
| **`errors[]` in every sync** | Human-readable strings, stored in the DB, surfaced by the status API. "interface fa:16:3e:... absent after 90s", not `E_NO_IFACE` |
| **Console breadcrumbs** | The agent writes lifecycle events to `/dev/console`, so `openstack console log show` works **with no network at all**. Proven in our own lab — experiment 06 diagnosed entirely from console markers |
| **`neutron-appliance-debug <router-id>`** | One admin command that dumps: the DB row, the Nova instance state and fault, the port list, `last_seen` age, desired versus applied generation, the rendered document with secrets redacted, and the tail of the console log |
| **Agent-local CLI** | `neutron-appliance-agent status \| dump-config \| sync-now` for when you do have a shell |
| **Debug SSH key** | Injected only when `[appliance] debug_ssh_key` is set. **Off by default.** Document that the agent will overwrite manual changes on its next pass |

Server-side, log the rendered document at DEBUG with credentials redacted, and
log every state transition at INFO with the router ID.

---

## 8. Milestones

Each milestone is independently reviewable and leaves the tree working.

### M0 — Repository scaffolding
- `neutron_appliance` package skeleton per §3, Apache 2.0, standard OpenStack
  layout.
- **Python 3.9 is the syntax floor** (`Q-H`: RHOSO's RHEL 9 baseline). No
  `match`, no PEP 604 `X | Y` annotations at runtime — use
  `from __future__ import annotations` where they help readability. Set
  `requires-python = ">=3.9"` and configure the linters to enforce the target
  so violations fail CI rather than fail in RHOSO.
- Pin against the Neutron version RHOSO ships (**to verify** — get the exact
  version before M2, it decides which callback payload shapes apply).
- `tox.ini`: `py39`, `pep8`, `format`, `cover`, `docs`. **The `pep8` env runs
  ruff, not flake8** — see §12.1. The env keeps its name so that standard
  Zuul jobs, which simply invoke `tox -e pep8`, work unchanged.
- CI skeleton, unit test harness, `.gitreview`/CI config as appropriate.
- **Acceptance:** `tox -e pep8,py39` passes on an empty test suite, and
  `ruff format --check` is clean. Format the tree from the **first** commit so
  there is never a reformatting churn commit.

### M1 — `common`: the protocol
- `schema.py`: config document and sync request/response models, with
  explicit versioning and validation. No Neutron, no Nova.
- `protocol.py`: paths, state enum, error shapes.
- Round-trip and validation tests, including rejecting an unknown
  `schema_version` and tolerating unknown *fields*.
- **Acceptance:** the import-isolation test of §3.1 passes; 100% coverage of
  `common/` is realistic here and worth requiring.

### M2 — Server: driver and database
- `L3ServiceProvider` subclass that loads under `neutron-server`, declares
  `ha_support = UNSUPPORTED`, `distributed_support = UNSUPPORTED`, and claims
  flavored routers.
- Reject unsupported options at `BEFORE_*` with clear errors
  ([`../02-hooking-neutron.md`](../02-hooking-neutron.md) §2.2).
- DB model + alembic branch: router to instance mapping, generations, state,
  `last_seen`, credential hash, flavor snapshot.
- Registry callbacks that bump `generation` in the **same transaction** as the
  change. No Nova calls, no blocking.
- **Acceptance:** create and delete a flavored router with the worker
  disabled; the DB row appears and disappears; the OVN provider still works
  for unflavored routers.

### M3 — Server: the API extension
- `POST .../sync` and `GET .../{router_id}` per §4 and §6.
- Authentication per §4.2b: validate the `X-Appliance-Token` against the
  stored hash, bound to the `router_id` in the URL, compared in constant time.
- The `appliance-agent` role and the policy rules restricting it to these two
  endpoints. Ship a sample `policy.yaml`.
- Credential issue, and rotation with the previous-token grace window.
- **Acceptance:** `curl` with a valid service-user token and the right
  per-router token returns an empty document and records `last_seen`; the
  *wrong* router's token against the same URL is rejected; a rotation
  followed by a simulated agent crash still authenticates with the old token,
  and the old token stops working once the new one is used.

### M4 — Server: reconciler
- `PeriodicWorker` with jittered start, level-triggered: compare routers that
  should exist against instances that do.
- Management network and security group, created idempotently (§5).
- Service project handling and a Nova client using a service user.
- Boot the appliance with **one** NIC (management) and a config drive.
- Delete path, including orphan sweep, tagging every resource with the router
  ID.
- Bounded retries with backoff; build before destroy; a circuit breaker.
- **Acceptance:** router create produces a VM; router delete removes it and
  every port; killing the worker mid-create and restarting converges.

### M5 — Agent
- Bootstrap from config drive, on-disk state cache, sync loop with backoff and
  jitter, console breadcrumbs, `noop` backend.
- systemd unit, `Restart=always` with backoff. **No watchdog that reboots the
  VM** — the agent is not load-bearing for forwarding.
- **Acceptance:** against a stub server, the agent syncs, rotates a
  credential, survives the server disappearing for 10 minutes, and reapplies
  cached config across a reboot with the server still down.

### M6 — Appliance image
**Decided (`Q-E`): diskimage-builder**, the same tooling Octavia uses for
amphorae.

- A `image/` directory with a DIB element installing the agent, its systemd
  unit and the minimum runtime (Python 3.9, `iproute2`, `chrony`).
- RHEL 9 / CentOS Stream 9 base, matching the `Q-H` baseline.
- **No per-instance data baked into the image.** Everything instance-specific
  arrives on the config drive; every appliance boots from a byte-identical
  image.
- `chrony` enabled: token expiry and TLS validation both depend on the clock.
- Record provenance in the image and in a build manifest: base image, agent
  version and commit, package versions, build command.
- **Acceptance:** two builds from the same inputs produce the same manifest;
  the image boots and the agent starts and logs to the console with no
  network present.

### M7 — devstack plugin and smoke test
- `devstack/plugin.sh` installing the driver, registering the provider
  **without `:default`**, creating the flavor and profile.
- A scripted run of the §1 acceptance scenario.
- **Acceptance:** one command from a clean devstack to a converged appliance.

### M8 — Debuggability and documentation
- `neutron-appliance-debug`, the agent CLI, and the operator documentation
  from §7.
- **Acceptance:** deliberately break a router three ways (no image, wrong
  endpoint, agent crash-looping) and diagnose each one using only the tools,
  without SSH.

---

## 9. Testing strategy

| Layer | What |
|---|---|
| Unit | `common/` exhaustively; driver callbacks with Neutron's unit test bases; reconciler against a fake Nova |
| Lint and format | `ruff check` and `ruff format --check` in the `pep8` env (§12.1) |
| Import isolation | §3.1 — the agent must not pull in Neutron. Enforced twice: `ruff` banned-api at edit time, and a runtime import test for transitive cases |
| Contract | One set of fixtures in `common/` exercised by both server and agent tests, so the two halves cannot drift |
| Functional | devstack job running the §1 scenario |
| Manual | The lab in the `neutron-stf-poc` repository, which already has eight scripted experiments and a known-good devstack |

Note for the functional job: there is **no functional or tempest coverage of
the router-flavor path anywhere in the Neutron tree**, so this job has
upstream value on its own, and it is what will catch Neutron breaking us —
which is how Astara died.

A lab trap worth inheriting: devstack's `br-ex` loses its address across a
host reboot, which silently breaks every data-path test. Check it as a
precondition in any functional job.

---

## 10. Documentation to copy

Copy into the new repository's `docs/`, **with the §2 changes applied**:

| Source | Action |
|---|---|
| `docs/intro.md` | Adapt: goal, terminology, the claim-tagging convention. Keep the tagging convention — it is the main reason these documents aged well |
| `docs/01-goal.md` | Copy nearly as-is |
| `docs/02-hooking-neutron.md` | Copy. Still accurate, including port ownership and the two senses of "attach" |
| `docs/03-configuration.md` | **Rewrite §3.3 and §3.4** for the pull model. Keep the document shape, generation semantics, observability |
| `docs/04-first-draft.md` | Copy as the stage 0 specification |
| `docs/05-lifecycle.md` | Copy; note that `Q1` is decided for now |
| `docs/06-performance.md` | Copy; nothing happens here until stage 0 works |
| `docs/open-questions.md` | Copy and update: close `Q1`, `Q4`, `Q9`; add the `Q-A`..`Q-H` below |
| `docs/findings.md` | Copy **unchanged**. The vendordata results stay true even though we are not using the channel — record that we chose not to, and why |
| `docs/experiments.md` | Copy unchanged |
| `docs/prior-art.md` | Copy unchanged. Octavia's amphora agent is the closest prior art to what M5 builds |
| `docs/archive/` | Do **not** copy |

Add one new document, `docs/protocol.md`: the §4 contract, as the normative
reference for both halves.

---

## 11. Questions

### 11.1 Decided

| # | Question | Decision |
|---|---|---|
| **Q-A** | How does the agent authenticate to the sync endpoint? | **Shared Keystone service user for admission, per-router token for authorisation** (§4.2b). No `api-paste.ini` change. Requires the restricted `appliance-agent` role and the rotation grace window |
| **Q-E** | How is the appliance image built? | **diskimage-builder**, RHEL 9 / CentOS Stream 9 base (M6) |
| **Q-H** | Target Python and Neutron versions? | **RHOSO's RHEL 9 baseline: Python 3.9 syntax floor**, pinned to the Neutron version RHOSO ships (exact version **to verify** before M2) |

### 11.2 Proceed on the recommendation unless told otherwise

Not blocking. Implement the recommendation and record it; raise it again only
if an assumption breaks.

| # | Question | Recommendation |
|---|---|---|
| **Q-B** | Bootstrap via config drive or user data? | **Config drive.** It needs no metadata service and no network, which matters because the agent must work before the management network is up |
| **Q-C** | One distribution, or separate `neutron-appliance` and `neutron-appliance-agent`? | **One for pre-0**, with the import-isolation test holding the boundary. Split when the image build makes it necessary |
| **Q-D** | May the agent depend on `oslo.config` / `oslo.log`? | **Yes**, plus `keystoneauth1`, which §4.2b now requires. Avoid anything that drags in Neutron |
| **Q-F** | Repository name and hosting; upstream from day one, or internal first? | Upstream-shaped from day one even if hosted internally: Apache 2.0, `.gitreview`-compatible layout, no internal references in code or docs |
| **Q-G** | One shared management network, or one per router? | **One shared**, in the service project. Per-router multiplies Neutron objects for no isolation benefit that the security group does not already give |

---

## 12. Conventions

- Apache 2.0, `tox -e pep8` clean.
- **Python 3.9 syntax floor**, enforced by the linter configuration, not by
  reviewer memory.

### 12.1 Linting and formatting: ruff

**Use `ruff` for both linting and formatting. Do not use flake8, hacking,
pycodestyle, isort or black.**

```toml
# pyproject.toml
[tool.ruff]
target-version = "py39"      # also enforces the Q-H syntax floor
line-length = 79             # matches the Neutron code read alongside this

[tool.ruff.lint]
select = [
    "E", "W",   # pycodestyle
    "F",        # pyflakes
    "I",        # import sorting, replaces the H3xx import checks
    "B",        # bugbear: mutable defaults, and other real bugs
    "UP",       # pyupgrade, bounded by target-version
    "C4",       # comprehensions
    "G",        # logging format
    "TID",      # banned imports, see below
    "RUF",      # includes RUF100, unused noqa
]

[tool.ruff.lint.isort]
known-first-party = ["neutron_appliance"]

[tool.ruff.format]
quote-style = "single"       # consistent with the OpenStack code around it
```

`target-version = "py39"` does the job M0 asks for properly: 3.10+ syntax is
rejected at lint time rather than discovered in RHOSO, and `UP` will not
rewrite code into syntax the target cannot run.

**Enforce the package boundary statically.** Ruff resolves the nearest
configuration file per directory (**measured** with ruff 0.17.5: a stray
`import neutron` under `agent/` raises `TID251`), so a second config under the
agent makes the §3.1 rule a lint error at edit time, not just a test failure
at CI time:

```toml
# neutron_appliance/agent/ruff.toml  (and common/ruff.toml)
extend = "../../pyproject.toml"

[lint.flake8-tidy-imports.banned-api]
"neutron".msg = "the agent must not import Neutron; put shared code in common/"
"neutron_lib".msg = "the agent must not import neutron-lib"
```

Keep the runtime import test from §3.1 as well. The lint catches the obvious
case early; the test catches transitive imports the linter cannot see.

**What ruff does not give you, and what to do about it.** Neutron's own
hacking layer is 13 checks (`neutron/hacking/checks.py`), and most are either
obsolete for a new codebase (`no_import_six`) or expressible as `TID`
banned-api entries (`no_import_packaging`, `no_sqlalchemy_event_import`). Two
gaps are real:

| Gap | Handling |
|---|---|
| Apache 2.0 licence header (hacking H102/H103) | Ruff has `CPY001` in preview (**to verify**). If it is not usable, a pre-commit hook or a six-line unit test walking the tree is enough. Worth keeping — it is a packaging and legal expectation, not a style preference |
| i18n idioms (`_()` usage, `oslo_i18n`) | Ruff cannot check these. Decide whether this project needs translated log or exception strings at all; for an out-of-tree driver the answer is probably no, in which case the checks are moot |

**If and when the repository goes to OpenDev**, reviewers may expect
`hacking`. Adding it back later is cheap: the `pep8` env name is already what
Zuul invokes, so it becomes a question of adding a second command to that env
rather than rewiring CI. Do not pre-emptively carry the old toolchain for a
hypothetical review that has not happened.
- No emojis anywhere, in code, comments, logs or documentation.
- Commit messages: imperative mood, a body explaining *why*, `Signed-off-by`.
- Tag every factual claim in documentation as **measured**, **code-read**,
  **assumed** or **to verify**, per `docs/intro.md`. Untagged text is design
  intent.
- Every `file:line` reference names the tree and commit it was read against.
- When an experiment contradicts a document, change the document and record
  the correction. The corrections table in `findings.md` is a feature.
