<sub>[Index](intro.md) · [Experiments](experiments.md) · [Open questions](open-questions.md)</sub>

# Findings

What the lab established, organised by the design question it answers. The
per-run detail — setup, method, raw numbers, traps — is in
[experiments.md](experiments.md); this page is the conclusions.

Environment for every result below: devstack 2027.1 (`902e73e50c24`),
Ubuntu 24.04 Noble, ML2/OVN, OVN 24.03.8 / OVS 3.3.9, single node,
8 vCPU / 12 GB. Raw output under `/opt/stack/lab/results/` in the VM.

## Configuration delivery

**Nova dynamic vendordata is a live, per-request channel.** 40 guest fetches
produced 40 distinct responses and 40 calls to our service, with zero repeats.
A config change was visible to the guest in **1 second**. The design in
[§3.3](03-configuration.md) holds as written.

**`metadata_cache_expiration` does not cache vendordata.** The 15 s default
was in force throughout and had no effect on `vendor_data2.json`. This was the
main risk flagged in earlier drafts and it is not real.

**The channel is genuinely boot-independent.** Metadata reaches a guest on an
isolated network with no router at all, through the per-network OVN
`localport`. An appliance can therefore read its configuration before it has
any gateway or external connectivity — which is the whole argument for
vendordata over a management network.

**The response is namespaced by target name.** Nova does not relay our JSON
verbatim; it wraps each dynamic target under its configured name, alongside
StaticJSON's output: `{"static": {}, "router": {...}}`. The agent must unwrap
`["router"]`. Not in any earlier draft, and it belongs in the agent contract.

**The blast radius is narrow.** Nova invokes the vendordata provider only for
`vendor_data2.json`, never for `meta_data.json`. A 2000 ms delay injected into
our endpoint moved `meta_data.json` latency by **0 ms** and `vendor_data2.json`
by exactly +2000 ms. With the endpoint stopped, every request on both
endpoints still succeeded. The residual coupling is real but bounded:
cloud-init fetches `vendor_data2.json` once per instance boot, so a slow
endpoint slows every instance's boot by that much.

Still unmeasured: the cost at cloud scale. The lab had one VM, so the
"unknown instance" path was never exercised in bulk
([§6.4](06-performance.md) C6).

## Port ownership

**The `project_id` problem is not gateway-only.** With the appliance in a
dedicated service project, Nova rejects *both* Neutron router ports:

| Port | `project_id` | Source |
|---|---|---|
| `network:router_interface` | the tenant's | `l3_db.py:958` |
| `network:router_gateway` | empty | `l3_db.py:377` |
| `network:floatingip` | empty | `l3_db.py:1545` |

Nova's check is an unconditional string comparison with **no policy hook**
(`nova/network/neutron.py:756-759`), and `tenant_id` on a port is
`allow_put: False`, so there is no API path to repair it afterwards. The size
of the problem is set by where the VM lives, not by which port type it is.

**The fix works.** An ipless (`fixed_ips: []`), port-security-off port created
in the service project attaches cleanly; ML2/OVN binds it (`up: true`,
`type: ""`, `addresses: [unknown]`, `port_security: []`); and the guest,
carrying an address that Neutron allocated to a *different project's* port
which is attached to nothing, reached the external gateway and was reachable
from the host at 0% loss. No raw SQL, no upstream change, no extra IP
consumed. See [§2.9](02-hooking-neutron.md).

**But ports must be hot-attached, never boot-attached.**

```
400 ... POST /compute/v2.1/servers,
Port <id> requires a FixedIP in order to be used.
```

`PortRequiresFixedIP` lives in `validate_networks` (the boot path), not in the
hot-attach path, and is skipped only when `ip_allocation == 'none'`. The
`ip_allocation` extension is **not loaded** on this deployment — 104 network
extensions, none of them that one — so the API reports `None` and Nova can
never see `'none'`. The appliance therefore always boots with exactly one NIC
and acquires the rest by hotplug.

**This is a deployment property, not a law** (**code-read**). The attribute is
read-only, so it is earned by passing `fixed_ips: []`, never requested. ML2's
core plugin never advertises the extension; the OVN mech driver merely permits
it (`supported_extensions()` intersects with `ML2_SUPPORTED_API_EXTENSIONS`,
which lists it); the only component that actually advertises it is the
**`segments` service plugin** (`neutron/services/segments/plugin.py:74`). The
lab devstack does not enable `segments`. If RHOSO does, boot-attach of ipless
ports should work there — untested, and the design should not depend on it.

## ML2/OVN behaviour

**The phantom Logical_Switch_Port is inert.** For a flavored router's
interface port, ML2/OVN creates an LSP with `type: ""`, `addresses: [unknown]`,
`port_security: []`, `up: false` — versus `type: router`,
`addresses: [router]`, `up: true` for a normal OVN router. With no `MAC IP`
entry there is no ARP responder flow for the gateway IP, so it does **not**
blackhole traffic. It is a lying row that `ovn_db_sync` and the maintenance
task reconcile over.

Consequence for upstream: the `requires_lsp` proposal is framed as **"remove a
hack"**, not "fix a traffic bug". Weaker than hoped, but the delete/recreate
dance in `UserDefinedNoLsp` is still unnecessary.

**Flavored router interface ports sit at Neutron status `DOWN` indefinitely**,
because nothing binds them. Relevant to what operators see
([`Q8`](open-questions.md)).

**Registering providers without `:default` leaves `ovn` as the default**,
exactly as predicted. Appending `:default` makes the built-in provider
silently disappear.

## Management network

**Option (b) works in both directions** — a tenant network whose gateway is a
default-flavour OVN router, with no recursion because the management router is
OVN's and not ours. VM to API, VM to metadata, host to VM by floating IP, and
host to VM directly via a host route all succeeded.

The reverse direction needs exactly two things: a route on the control-plane
side and a security group rule permitting the status port. Notably **SNAT does
not need to be disabled**, unlike octavia-operator's link router, because SNAT
applies to tenant egress and not to ingress initiated from the control plane.

So a controller-initiated status channel is achievable without the Octavia
`lb-mgmt-net` apparatus — no Multus attachments, no reserved controller IPs,
no bidirectional host routes. Option (a), an operator-provisioned provider
network, remains the answer where the control plane cannot hold a route into a
tenant overlay.

## Flavors and sizing

**Service-profile `metainfo` is usable for per-flavor VM sizing.** It
round-trips byte-exact and accepts a 4000-byte payload (the column is
`String(4096)`). `get_flavor_next_provider()` returns only
`{driver, provider}`, so a driver must read the service profile directly.

Gotcha: the API field is `metainfo` but the CLI and SDK expose the column as
`meta_info`. Write with `--metainfo`, read with `-c meta_info`.

## Corrections

Claims from earlier drafts that did not survive contact with the lab. Kept
because the pattern matters more than the individual errors.

| Claim | Status |
|---|---|
| "`metadata_cache_expiration` may bound convergence latency" | **Wrong** — no caching of vendordata at all |
| "The phantom LSP blackholes gateway traffic via the ARP responder" | **Wrong** — confirmed inert by measurement |
| "Vendordata responses are relayed verbatim" | **Wrong** — namespaced under the target name |
| "Our service sits in the metadata hot path for every instance" | **Overstated** — only `vendor_data2.json` requests reach it |
| "Management option (b) cannot do controller to guest" (said mid-run) | **Wrong** — it can; the failures were a security group |
| "The `project_id` problem affects the external gateway port" | **Understated** — with the VM in a service project, every router port is rejected |
| "A Nova policy exception for the service user could fix it" | **Wrong** — the check has no policy hook at all |
| "An ipless port avoids the fixed-IP requirement" | **Half right** — true on hot-attach, false at boot unless `ip_allocation` is loaded |
| "Is the raw-SQL `project_id` workaround still needed?" | **Was yes**, now obsolete — proposal A replaces it |
| "The phantom LSP is `type=''`, `addresses=['unknown']`, unbound" | **Confirmed** |
| "Vendordata is live per-request" | **Confirmed** |

Three of these were produced by experiments that initially reported a
*confident* wrong answer. The methodology notes in
[experiments.md](experiments.md) record how each was caught; they are the most
reusable thing in this repository.
