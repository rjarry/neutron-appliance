<sub>[Index](intro.md) · [&larr; Goal](01-goal.md) · [Configuration and state &rarr;](03-configuration.md)</sub>

# 2. Hooking into Neutron

## 2.0 Where the framework lives in the source

Everything below was read against `openstack/neutron` (`e5c7ddafc9a1`) and
`openstack/neutron-lib` (`a5c3d7d00049`), November 2026
(**code-read**). Keep these to hand; the framework is spread thin.

| Concern | Location |
|---|---|
| Parse `service_provider` lines | `neutron/services/provider_configuration.py:159-198` |
| Driver string to class | `provider_configuration.py:135-156` (stevedore alias, falling back to the literal path) |
| Load drivers | `neutron/services/service_base.py:26-69` |
| Driver to provider-name reverse lookup | `neutron/db/flavors_db.py:217-251` |
| Dispatch by flavor | `neutron/services/l3_router/service_providers/driver_controller.py:217-225` |
| Provider-to-resource table | `neutron/db/models/servicetype.py:21-26` |
| OVN provider configuration wrapper | `neutron/services/ovn_l3/service_providers/driver_controller.py:52-65` |
| Gateway / interface / FIP port creation | `neutron/db/l3_db.py:377`, `:958`, `:1545` |
| Nova's port ownership check | `nova/network/neutron.py:756-759` |
| Nova's fixed-IP requirement at boot | `nova/network/neutron.py:2558-2580` |
| Nova's PCI claim on interface attach | `nova/compute/manager.py:8401` |
| Router interface validation | `neutron/db/l3_db.py` `_validate_interface_info`, `_check_router_port` |
| Router API attributes | `neutron_lib/api/definitions/l3.py`, `l3_flavors.py`, `l3_ext_gw_mode.py` |
| Callback priorities | `neutron_lib/callbacks/priority_group.py:26-34` |
| `ip_allocation` definition and advertiser | `neutron_lib/api/definitions/ip_allocation.py:39-46`, `neutron/services/segments/plugin.py:74` |

## 2.1 Registration traps

- **Never append `:default`.** With the OVN L3 plugin,
  `_OvnPlusProviderConfiguration` catches the resulting `Invalid` at DEBUG
  level and the built-in `ovn` provider silently disappears, producing a later
  `KeyError` on `self.drivers['ovn']`
  (`ovn_l3/service_providers/driver_controller.py:52-65`). **Measured**:
  without `:default`, `ovn` correctly stays the default.
- Only the **first** `FlavorServiceProfileBinding` is used; `_validate_driver()`
  does not filter by service type; a disabled profile raises at *router create*
  time, not at flavor create time.
- Stage 2 reuses the mechanism with the `VPN` service type pointing at the same
  appliance. Whether `vpnservice` supports `flavor_id` the way routers do is
  **to verify**; if not, the VPN provider is deployment-wide and must itself
  check whether the underlying router is ours.

## 2.2 The API surface, and rejecting what we cannot do

`POST /v2.0/routers`, from the `neutron_lib` API definitions (**code-read**):

| Attribute | POST | PUT | Extension |
|---|---|---|---|
| `name`, `description`, `admin_state_up` | yes | yes | base |
| `external_gateway_info` (holds `enable_snat`) | yes | yes | `router`, `ext-gw-mode` |
| `flavor_id` | yes | **no** | `l3-flavors` |
| `ha` / `distributed` | yes | yes | `l3-ha` / `dvr` |
| `availability_zone_hints` | yes | **no** | `router_availability_zone` |
| `enable_ndp_proxy` | yes | yes | `l3-ext-ndp-proxy` |
| `enable_default_route_bfd` / `_ecmp` | yes | yes | respective extensions |
| `routes` | **no** | yes | `extraroute` |
| `service_type_id` | yes | no | vestigial Grizzly-era shim, implemented by nothing. Do not build on it |

Consequences:

1. **`flavor_id` is create-only** — no migration between providers.
2. **`ha` and `distributed` are effectively create-only** for flavored routers:
   `_update_router_provider()` raises `InvalidInput`.
3. **No way to pass arbitrary per-router options.** No "give me 4 vCPUs" (§2.8).

Because of (2), the capability flags must be right the first time — a wrong
declaration produces a router that can only be deleted, never fixed:

```python
class ApplianceL3Driver(base.L3ServiceProvider):
    ha_support          = base.UNSUPPORTED   # stage 3 may change this
    distributed_support = base.UNSUPPORTED   # structurally never supported
```

Anything outside the current stage must be **rejected at `BEFORE_*` with a
clear error**, never accepted and ignored. The failure to avoid is a tenant
setting `enable_snat=true` in stage 0, getting a 200, and finding out days
later that nothing egresses.

## 2.3 Events we receive

Subscribe with `@registry.has_registry_receivers` and
`@registry.receives(resource, [events], priority)`. Relevant resources:
`ROUTER`, `ROUTER_INTERFACE`, `ROUTER_GATEWAY`, `FLOATING_IP`, `PORT`,
`SUBNET`. Priorities, smallest first
(`neutron_lib/callbacks/priority_group.py`, **code-read**):

```
PRIORITY_ROUTER_EXTENDED_ATTRIBUTE = PRIORITY_DEFAULT - 100
PRIORITY_ROUTER_DEFAULT            = PRIORITY_DEFAULT      (55550000)
PRIORITY_ROUTER_CONTROLLER         = PRIORITY_DEFAULT + 100
PRIORITY_ROUTER_DRIVER             = PRIORITY_DEFAULT + 200
```

The payload type varies by publisher, which is easy to get wrong
(**code-read**):

| Publisher | `states[0]` is |
|---|---|
| `ROUTER BEFORE_CREATE` / `PRECOMMIT_CREATE` | API dict |
| `ROUTER AFTER_CREATE` / `AFTER_UPDATE` / `AFTER_DELETE` | `_make_router_dict()` dict |
| `ROUTER PRECOMMIT_DELETE`, `ROUTER_INTERFACE *`, `ROUTER_GATEWAY *` | `l3_models.Router` **ORM object** |
| `FLOATING_IP AFTER_*` | `_make_floatingip_dict()` dict |

Gaps and non-`ROUTER` sources, each a silent misconfiguration if missed:

| Item | Detail |
|---|---|
| Multiple-gateway API | `add/remove/update_external_gateways` publish **nothing**; `l3_extra_gws_db.py` has TODOs proposing `ROUTER_EXTRA_GATEWAY`. We see only a trailing `ROUTER AFTER_UPDATE` |
| Extra routes | Arrive via `ROUTER PRECOMMIT_UPDATE` with `routes_added` / `routes_removed` injected into the body (`extraroute_db.py:75-79`) |
| Floating IP status | `AFTER_STATUS_UPDATE` is published only by the OVN L3 plugin. `create_floatingip()` forces status DOWN for all FIPs and expects the flavor driver to call `update_floatingip_status()` — otherwise FIPs stay DOWN forever |
| Router `status` | **No supported way to write it.** Set at creation, never moves. Astara needed a private extension (`dhrouterstatus`). See [§3.5](03-configuration.md#35-tls-and-secrets) |
| `SUBNET AFTER_UPDATE` / `AFTER_DELETE` | `gateway_ip` or `host_routes` change; interface must go away |
| `NETWORK AFTER_UPDATE` | **MTU change** — interface MTU must follow or Geneve starts fragmenting |
| `PORT AFTER_UPDATE` | `allowed_address_pairs`, `fixed_ips` on ports we own |

Events are a *change notification*, not a transport. We use them to recompute
state, never as a queue to replay.

Unaffected, and it surprises people: **tenant instances behind our router still
get DHCP and metadata normally** — OVN serves both per-network through a
`localport`, independent of routing (**measured**, experiment 02).

## 2.4 Synchronous versus asynchronous, and where the work runs

Registry callbacks run inside the `neutron-server` API worker handling the
request, and `PRECOMMIT_*` runs inside the open DB transaction. Blocking there
is what makes the PoC fragile — it calls Nova and the VM with blocking
`requests` and `time.sleep` from API and RPC threads.

> **On the API path the driver computes desired state and commits it. Nothing
> else. It never calls Nova. It never calls the VM.**

| Work | Where | When |
|---|---|---|
| Validate, reject unsupported options | driver, `BEFORE_*` | sync |
| Recompute the document, bump `generation` | driver, `PRECOMMIT_*`, same transaction | sync |
| Boot the VM, attach ports, wait for anything | manager | async |
| Collect health | manager | async, periodic |
| Serve the config to the guest | vendordata endpoint | on guest request |

So `openstack router create` returns as soon as the row and document are
committed; the VM does not exist yet. That is unavoidable — Octavia documents
5 minutes to ACTIVE plus 10 more for the agent, and putting boot latency on the
API critical path is among the reasons Astara died.

The async half is a **reconciler**: a level-triggered loop converging "routers
that should exist" against "VMs that do exist". Two homes:

| Option | Pros | Cons |
|---|---|---|
| a. `PeriodicWorker` in `neutron-server` | Nothing new to package; DB session and config for free | Shares a process with the API; a slow Nova call holds a worker; no control over scaling or leader election; drags the vendordata endpoint into the API workers |
| b. A separate **appliance manager** service | Isolated failure domain, independent scaling, hosts the vendordata endpoint, natural home for a state machine | A new service to package, deploy and operate; in RHOSO an operator change |

**Recommendation: (b)** ([`Q1`](open-questions.md)). The vendordata endpoint must be fast and
reachable by `nova-api-metadata`; the reconciler must make slow Nova calls;
and §5 grows a state machine and a health loop regardless. Stated honestly,
this is where we stop being a plugin and become a service. If that cost is
refused, (a) survives stage 0 provided the endpoint is split out anyway — it
is stateless and read-only and should scale horizontally behind the normal API
load balancer whichever shape the reconciler takes.

## 2.5 Service project, quota and the cost model

**Appliance VMs should live in a dedicated service project**, as Octavia does:
a user who can `openstack server delete` their own router is a support
incident generator, and a router must not consume tenant instance quota.

**This choice is not free, and it is coupled to §2.9.** Nova refuses to attach
a port whose `project_id` differs from the instance's, and the three port types
we care about have three different owners (`neutron/db/l3_db.py`,
**code-read**):

| Port | `project_id` | Line |
|---|---|---|
| `network:router_interface` | the **tenant's** (`router.project_id`) | 958 |
| `network:router_gateway` | **empty** (`'project_id': ''  # intentionally not set`) | 377 |
| `network:floatingip` | **empty** | 1545 |

So the decision determines the size of the problem:

| VM lives in | Consequence |
|---|---|
| The **tenant's** project (what the PoC did) | Internal interfaces attach unmodified; only the gateway port fails |
| A **service** project (recommended here) | **Every** port has the wrong owner, internal interfaces included |

The service project is the right answer, and §2.9's fix — **validated by
experiment 08** — generalises cleanly to all three port types, so the larger
scope costs nothing extra.

A Neutron router used to cost a few OVN rows and now costs a VM. Nothing in
Neutron knows that, and it is the likeliest production surprise:

| Concern | Position |
|---|---|
| Tenant quota | The tenant pays the *router* quota, not Nova quota. The default of 10 now authorises 10 VMs per tenant. Deployments offering this flavor must revisit it |
| Service project quota | Sized for the whole cloud; exhausting it fails router creation for everyone. A first-class alarm, not a surprise |
| Who may use the flavor | Network flavors have no project scoping — any tenant can pass any `flavor_id` (**to verify**). Restricting it has no obvious in-tree answer ([`Q19`](open-questions.md)) |
| Billing | Appliance VMs in a service project are invisible to tenant usage reporting |
| Diagnostics | The tenant cannot see the VM, so everything user-facing must come through Neutron ([§3.6](03-configuration.md#36-observability-and-operator-access)) |

A pleasant consequence of the vendordata design: **appliances need no
per-instance bootstrap data at all** — no user-data, no cloud-init template,
no injected keys, no baked-in controller address. Every appliance boots from
an identical image with an identical Nova request bar the port list, removing
a whole class of "booted with the wrong config" bugs that Astara and Octavia
both have.

## 2.6 Placement

- **`availability_zone_hints`** is create-only and currently meaningless for
  flavored routers — `get_router_availability_zones()` queries the OVN logical
  router unconditionally and returns `[]` (**code-read**). Map it to the
  **Nova AZ** the appliance boots into; that gives the attribute a real meaning
  for the first time.
- **Anti-affinity** matters even without HA: fifty tenants' routers on one
  compute is a correlated failure nobody asked for. Nova server groups,
  `soft-anti-affinity` — soft, because hard would cap router count at compute
  count. Stage 3 needs *hard* anti-affinity within each HA pair.
- **Density** (how many appliances before tenant workloads suffer) is a
  benchmark input, [§6.4](06-performance.md#64-control-plane-and-scale-benchmarks) C7.

## 2.7 Port attachment

### Two meanings of "attach"

The word does two unrelated jobs in this design, and conflating them wastes
hours:

| | **Neutron sense** | **Nova sense** |
|---|---|---|
| Operation | `add_router_interface` | `interface_attach` |
| CLI | `openstack router add subnet\|port` | `openstack server add port` |
| Effect | Registers a port as a *logical* router interface | Hot-plugs a *vNIC* into a VM |
| Port needs a fixed IP? | **Yes, mandatory** | No (on hot-attach) |
| Who does it here | Neutron, as pure IPAM | the manager, for forwarding |

Both happen, to **different ports**. Experiment 08's rejections were the Nova
sense; Neutron was perfectly happy with those ports.

### How Neutron attaches a port to a router

`PUT /routers/{id}/add_router_interface` takes **either** `subnet_id` **or**
`port_id`, never both (`_validate_interface_info`, **code-read**). The
external gateway is a third, separate mechanism that takes a *network*:

| Operation | Input | Port created by | Address chosen by | `device_owner` | `project_id` |
|---|---|---|---|---|---|
| `router add subnet` | `subnet_id` | **Neutron** | the subnet's `gateway_ip` | `network:router_interface` | the router's (tenant) |
| `router add port` | `port_id` | **you**, beforehand | you | `network:router_interface` | the port creator's |
| `router set --external-gateway` | `network_id` | **Neutron** | IPAM, or your `external_fixed_ips` | `network:router_gateway` | **empty** |

There is no "add network to a router interface" operation, and
`external_gateway_info` has **no `port_id` field** — its validator accepts
only `{network_id (required), external_fixed_ips (optional)}`
(`neutron_lib/api/definitions/l3.py:92-104`), with `enable_snat` bolted on by
`l3_ext_gw_mode.py:42-48` (**code-read**). You therefore cannot hand Neutron a
gateway port you made yourself, which is precisely why [§2.9](#29-port-ownership-the-project_id-problem)
leaves that port alone instead of trying to fix its owner.

A port offered by `port_id` must satisfy `_check_router_port`:

```python
if port['device_id'] != device_id:
    raise n_exc.PortInUse(...)
if not port['fixed_ips']:
    msg = _('Router port must have at least one fixed IP')
    raise n_exc.BadRequest(resource='router', msg=msg)
```

**So an ipless port can never be a Neutron router interface.** The two senses
of attach have opposite requirements, and that is exactly why splitting the
roles works: Neutron's port has the IP and does IPAM; our ipless port has no
IP and does the forwarding. Neither tries to do both.

### How we attach a port to the appliance

1. Neutron creates the `network:router_interface` port holding the gateway IP,
   and it stays attached to nothing.
2. The manager creates its own ipless port and hot-plugs it with
   `nova interface_attach`.
3. The agent resolves the NIC **by MAC** and configures the address from the
   document.

| Sharp edge | Detail |
|---|---|
| **Every data port must be hotplugged** | **Measured** (experiment 08): Nova rejects an ipless port at `servers.create()` with `PortRequiresFixedIP`, but accepts it on attach. The appliance always boots with one NIC (management) and acquires the rest by hotplug, so this path is load-bearing, not an optimisation. See §2.9 |
| **Hotplug is async with no completion signal** | Octavia polls, 404s retryably, and writes `/sys/bus/pci/rescan` because "nova hot plugs the interface but the kernel doesn't get the memo". Astara destroyed and rebooted the VM after 10 s — its worst subsystem. Our pull model helps: the agent just does not advance its generation until the NIC exists. Retry limit is [`Q6`](open-questions.md) |
| **vNIC count bounds interfaces per router** | Roughly 15-30 by machine type (**assumed**). The price of dropping trunk ports; document it as a limit rather than let users discover it |
| **SR-IOV is possible on the Nova side** | Nova claims a PCI device during attach (`_claim_pci_device_for_interface_attach`, `compute/manager.py:8401`, **code-read**), so a VF can be hot-plugged into the appliance. Constraints: a VF cannot be a trunk parent, PCI and NUMA availability bound placement, and live migration gets harder ([§5.2](05-lifecycle.md#52-failure-handling-and-currently-undefined-behaviour)). Note that `vnic_type` is meaningless on the *Neutron* router interface port — nothing ever binds it. **Untested**; only relevant if [§6](06-performance.md) shows the kernel missing the target |
| **ML2/OVN creates a phantom LSP** regardless of flavor | **Measured** inert: `type=""`, `addresses=[unknown]`, `port_security=[]`, `up=false`. No ARP responder entry, so it does **not** blackhole traffic. A lying row that `ovn_db_sync` reconciles over. In-tree `UserDefinedNoLsp` deletes it on `AFTER_CREATE` and recreates it on `BEFORE_DELETE`; since it is inert, whether to copy that dance is [`Q5`](open-questions.md), and the upstream framing is "remove a hack", not "fix a traffic bug" |
| **`allowed_address_pairs`** | Needed only when something other than the port's fixed IP is sourced from it — VRRP VIPs ([§5.4](05-lifecycle.md#54-ha-vrrp-versus-bfd)), possibly IPsec if the tunnel endpoint differs. **Not needed for stage 0**; earlier notes listed it as a blanket requirement without saying when |

### The phantom LSP, and the upstream fix worth proposing

Background for [`Q5`](open-questions.md) and [`Q15`](open-questions.md).
`ovn_client.create_port()` and `_get_port_options()` have no flavor check, so
ML2/OVN builds an LSP for a flavored router's interface port. The chain:
router ports are `network:*`-owned, so `is_port_trusted()` is true,
`_get_allowed_addresses_from_port()` returns empty, and the fallback at
`ovn_client.py:509-514` sets `addresses = ['unknown']` (**code-read**,
**measured**).

The no-LSP variant came from
[LP#2078382](https://bugs.launchpad.net/neutron/+bug/2078382) (Miguel Lavalle,
2024, merged `44cbbba369ad`), whose bug text describes our use case exactly:

> There is at least one OpenStack operator that requires the ability to create
> a ML2/OVN user defined router flavor that doesn't have Logical Switch Ports
> associated to their router interfaces. [...] **Neutron acts only as an IP
> address manager for the router interfaces**.

The commit is explicit that containment was a choice: *"The new functionality
is completely contained in the new driver, with no logic added to the rest of
ML2/OVN."* Hence the delete-on-create, recreate-on-delete dance in
`UserDefinedNoLsp`, which exists purely so ML2/OVN's normal delete path and
revision bookkeeping still work.

**Proposed fix.** `utils.is_lsp_ignored()` already exists for this class of
problem:

```python
def is_lsp_ignored(port):
    # Since the floating IP port is not bound to any chassis, packets from vm
    # destined to floating IP will be dropped. To overcome this, we do not
    # create/update floating IP port in OVN.
    return port.get('device_owner') in [const.DEVICE_OWNER_FLOATINGIP]
```

Add a `requires_lsp = True` class attribute on `L3ServiceProvider`, beside the
existing `ha_support` / `distributed_support` flags, consulted by ML2/OVN
through the driver controller. Backward compatible by default, and it removes
the dance entirely.

Reviewer objection to pre-empt: `is_lsp_ignored()` is also called on the
update and delete paths, and each call site would need the router resolved.
Note that `_is_ovn_router_flavor_port()` already pays exactly that cost in
`create_port_precommit`.

Someone else already wanted this; finding them is an action on OSPRH-40376.

### Trunk ports, if they ever come back

`neutron/services/trunk/drivers/ovn/trunk_driver.py:33-40` (**code-read**):

```python
SUPPORTED_INTERFACES = (portbindings.VIF_TYPE_OVS,
                        portbindings.VIF_TYPE_VHOST_USER)
SUPPORTED_SEGMENTATION_TYPES = (trunk_consts.SEGMENTATION_TYPE_VLAN,)
```

So an SR-IOV VF cannot be a trunk parent, and a VF with a single VLAN tag
gives exactly one network. Relevant if stage 4 revisits trunking.

## 2.8 VM sizing via `metainfo`

Since the API cannot carry per-router options, use **one flavor per sizing
profile** with the detail in the service profile's `metainfo`:

```bash
openstack network flavor profile create --enable \
  --driver my_pkg.drivers.ApplianceL3Driver \
  --metainfo '{"nova_flavor": "router.large", "image": "appliance-1.2"}'
```

**Measured**: round-trips byte-exact, accepts 4000 bytes (column is
`String(4096)`). Read by nothing in the L3 path — `get_flavor_next_provider()`
returns only `{driver, provider}` — so the driver reads the service profile
directly. Gotcha: the API field is `metainfo`, the CLI column is `meta_info`.

This is the only sanctioned extension point, and it gives operators "small
router" / "large router" / "vpn router" with no API change. It is also
operator-editable at runtime and does not retroactively resize anything, so
**snapshot the profile into the router's record at create time** — otherwise
an operator typo silently rebuilds every router on the next pass.

## 2.9 Port ownership: the `project_id` problem

**Measured, still broken on devstack 2027.1**, and larger than earlier drafts
of this document claimed. Scope is set by §2.5; the mechanism is the same in
every case.

```
400 ... POST /compute/v2.1/servers/<id>/os-interface
Port <port-id> not usable for instance <instance-id>
```

Nova's check is an **unconditional string comparison with no policy hook**
(`nova/network/neutron.py:756-759`, **code-read**):

```python
# Make sure the instance has access to the port.
if port['tenant_id'] != instance.project_id:
    raise exception.PortNotUsable(port_id=request.port_id,
                                  instance=instance.uuid)
```

It runs on boot and on attach alike, and it cannot be fixed after the fact
either: `tenant_id` on a port is `allow_post: True, allow_put: False`
(**code-read**), so there is **no API path to change a port's owner**. That is
why the PoC reached for raw SQL (`UPDATE ports SET project_id`), which is not
shippable — it bypasses the object layer, the OVN revision bookkeeping and any
caching, and no reviewer will take it.

> Correction to earlier drafts: "a Nova policy exception for the service user"
> was listed as a candidate. **No such policy exists.** Introducing one is a
> Nova code change (proposal C).

### Proposal A — stop attaching Neutron's ports (recommended)

Split the two jobs the port currently does:

| Role | Port |
|---|---|
| **IPAM record** — holds the address, backs `external_gateway_info`, anchors FIPs | Neutron's `network:router_*` port, left exactly as the L3 plugin made it, **never attached to anything** |
| **vNIC** — what Nova plugs into the VM | **our** port, created in the service project, `fixed_ips: []`, `port_security_enabled: False` |

The agent then configures the IPAM port's address onto the NIC it matches by
MAC — which is what it does anyway ([§3.1](03-configuration.md#31-the-document-and-the-generation-number), [§4.3](04-first-draft.md#43-the-agent)).

Two verified mechanics remove the obvious objection that this wastes
addresses:

- Passing `fixed_ips: []` **explicitly** sets `ip_allocation = 'none'`
  (`neutron/db/db_base_plugin_v2.py:1609-1612`, *"[] was passed explicitly as
  fixed_ips. An unaddressed port."*).
- Nova accepts such a port — `ipless_port = port.get('ip_allocation') ==
  'none'` (`nova/network/neutron.py:2566`) bypasses `PortRequiresFixedIP`.

So the cost is **one extra port row per interface, and zero extra IP
addresses**.

It needs no upstream change, works on shipped releases, and generalises to all
three port types, so it is the same mechanism whether the VM is in the tenant
or the service project. It also removes the existing hack where the manager
clears `device_owner` on Neutron's port to get Nova to accept it, and it fits
the LP#2078382 framing that for a user-defined flavor *"Neutron acts only as an
IP address manager for the router interfaces"*.

Costs, stated plainly:

- This **is** Astara's two-parallel-port-sets model. The defensible
  difference: Astara injected synthetic IPv6 link-local `fixed_ips` to fool
  Nova and matched ports by name regex; this uses a supported
  `ip_allocation=none` port and matches by MAC.
- Port quota per router roughly doubles.
- `port_security_enabled: False` on an external-network port needs
  justification. `allowed_address_pairs` is tighter but becomes noisy once
  stage 1 adds floating IPs.
- Attaching to an external network is policy-gated
  (`_check_external_network_attach`, `nova/network/neutron.py:588`), so the
  manager must be admin.

**Measured** (experiment 08): the whole chain works. Nova accepted the ipless
service-project port, ML2/OVN bound it (`up: true`, `type: ""`,
`addresses: [unknown]`, `port_security: []`), and the guest — carrying an
address allocated to a *different project's* port that is attached to nothing
— reached the external gateway and was reachable from the host, 0% loss. Both
Neutron router ports were rejected in the same run, confirming the baseline.

**One constraint found, and it shapes [§5.1](05-lifecycle.md#51-what-exists-to-reuse-and-how-large-a-daemon-we-need):**

```
400 ... POST /compute/v2.1/servers,
Port <id> requires a FixedIP in order to be used.
```

`PortRequiresFixedIP` is in `validate_networks` (boot), not in the hot-attach
path, and is skipped only when `ip_allocation == 'none'`. The
`ip_allocation` extension is **not loaded** on the lab deployment, so the API
reports `None` and Nova can never see `'none'`.

**Why it is absent, and how to get it back** (**code-read**): the attribute is
read-only (`allow_post: False, allow_put: False`,
`neutron_lib/api/definitions/ip_allocation.py:39-46`) — you never request it,
you earn `'none'` by passing `fixed_ips: []`. Advertising it is the question:

| Component | Advertises `ip_allocation`? |
|---|---|
| ML2 core plugin | **No** — 47 aliases in `_supported_extension_aliases`, not one of them |
| OVN mech driver | Permits it: `supported_extensions()` returns an **intersection** with `ML2_SUPPORTED_API_EXTENSIONS`, which lists it (`common/ovn/extensions.py:245`) |
| **`segments` service plugin** | **Yes** — `services/segments/plugin.py:74` |

So the extension appears when the `segments` service plugin is enabled, and
OVN will not filter it out. The lab devstack does not enable `segments`, which
fully explains the result. **If RHOSO enables `segments`, boot-attach of
ipless ports should start working** — worth checking, and worth a lab retest
before relying on it. Until then, assume hot-attach only.

> **The manager must hot-attach every data port after boot. It cannot
> pre-attach ports at `servers.create()`.** The appliance therefore always
> boots with exactly one NIC — its management interface — and acquires the
> rest by hotplug, which makes the hotplug path (§2.7) load-bearing rather
> than merely convenient. Check whether RHOSO loads `ip_allocation`; if it
> does, boot-attach may become possible, but do not design for it.

### Proposal B — put the VM in the tenant's project

Reverts §2.5. Internal interfaces then attach unmodified and only the gateway
and FIP ports need proposal A. Minimises the surface, at the cost of
everything §2.5 argues for — and Nova has no mechanism to stop a tenant
deleting their own instance. Keep as the explicit fallback.

### Proposal C — Nova upstream: make the check policy-gated

The precedent sits 170 lines earlier in the same file, for a closely related
ownership question:

```python
# nova/network/neutron.py:588
def _check_external_network_attach(self, context, nets):
    if not context.can(servers_policies.NETWORK_ATTACH_EXTERNAL, fatal=False):
```

Nova already policy-gates "may you attach to this network" and hard-codes "may
you attach this port". Proposing the same `context.can(..., fatal=False)`
shape around line 757 is small and consistent. Right fix, wrong timescale: it
helps no release we would ship against. Run it as a parallel track, start it
early, do not depend on it.

### Proposal D — Neutron upstream: give gateway ports an owner

Weakest. The empty value is deliberate (*"Port has no 'project-id', as it is
hidden from user"*), and populating it makes gateway ports tenant-visible and
quota-counted in every deployment. Only defensible narrowly, for flavored
routers, with the driver supplying the owner.

### Status

**Proposal A is validated and is the plan** (experiment 08). It needs no
upstream change, so proposal B is no longer required and proposal C becomes a
cleanup worth pursuing on its own merits rather than a dependency. Remaining
work: confirm the `ip_allocation` extension status in RHOSO, and decide
between port security off and `allowed_address_pairs` before stage 1 adds
floating IPs.
