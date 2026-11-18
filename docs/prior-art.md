<sub>[Index](intro.md) · [Findings](findings.md) · [Experiments](experiments.md)</sub>

# Prior art: Neutron L3 and LBaaS as service VMs

Two projects have already built what this repository is designing. One is dead,
one is alive. Both are worth knowing in detail before repeating their mistakes.

- **Astara** (formerly Akanda, DreamHost) — Neutron L3 routers as VMs.
  Retired 2016. This is literally our design, built a decade earlier.

- **Octavia** — load balancers as VMs ("amphorae"). Actively maintained, and
  the reference for the VM-and-agent half of our problem.

---

## Astara / Akanda

Repos (code lives at tags; `master` branches are retirement stubs):

| Component                  | URL                                            |
|----------------------------|------------------------------------------------|
| Orchestrator ("the RUG")   | https://opendev.org/openstack/astara           |
| Neutron plugins/extensions | https://opendev.org/openstack/astara-neutron   |
| In-VM appliance            | https://opendev.org/openstack/astara-appliance |
| Horizon panels             | https://opendev.org/openstack/astara-horizon   |

```
git clone https://opendev.org/openstack/astara
git checkout newton-eol
```

### Architecture

Four pieces: a multi-process orchestrator consuming Neutron notifications, a
set of Neutron plugin subclasses, a Debian appliance image, and Horizon panels.

The orchestrator ran three subsystems: an oslo.messaging listener that also
impersonated the **L3 agent's RPC surface** (`topic = l3_agent`), a scheduler
sharding events by tenant across worker processes each running a per-resource
state machine, and a health monitor injecting periodic `POLL` events.

### How it hooked into Neutron — and why that was fatal

**Not the service type framework. Not flavors.** Those did not exist for L3
when Astara was built. Instead it replaced both the core plugin and the L3
service plugin with subclasses:

```ini
core_plugin = astara_neutron.plugins.ml2_neutron_plugin.Ml2Plugin
service_plugins = astara_neutron.plugins.ml2_neutron_plugin.L3RouterPlugin
api_extensions_path = .../astara_neutron/extensions/
```

The subclasses rebound unbound methods onto classes to dodge the DVR and L3-HA
mixins, and mutated `supported_extension_aliases` at import time:

```python
class L3RouterPlugin(l3_router_plugin.L3RouterPlugin):
    # An issue in neutron is making this class inheriting some methods from
    # l3_dvr_db.L3_NAT_with_dvr_db_mixin. As a workaround we force it to use
    # the original methods in the l3_db.L3_NAT_db_mixin class.
    get_sync_data           = l3_db.L3_NAT_db_mixin.get_sync_data
    add_router_interface    = l3_db.L3_NAT_db_mixin.add_router_interface
    remove_router_interface = l3_db.L3_NAT_db_mixin.remove_router_interface
    _create_router          = l3_db.L3_NAT_dbonly_mixin.create_router
```

It also told Nova three deliberate lies: synthetic IPv6 link-local `fixed_ips`
injected into `_make_port_dict` so Nova would accept address-less ports; a
temporary `device_owner=network:router_interface` on the orchestrator's own tap
("lying here for IP") to make IPAM allocate; and port **name regex matching**
(`^(ASTARA|AKANDA):(MGT|VRRP):<uuid>$`) to decide port semantics.

Every Neutron release broke it.

**Router flavors exist now. This entire category of pain is avoidable, and
avoiding it is the single most important difference between Astara and us.**

### The appliance

Flask + gunicorn on **port 5000, entirely unauthenticated**, security resting
on the management network being private plus iptables. The important endpoint:

```
PUT /v1/system/config     # full desired state, ~90s timeout
GET /v1/firewall/rules    # used as the liveness probe
```

The config document carried networks, interfaces, subnets, allocations
(for dnsmasq host entries), floating IPs, ASN, and a `hostname`. Inside the VM:
iproute2, dnsmasq, bird6 (BGP + RAs), iptables, keepalived + conntrackd for HA,
strongSwan for VPNaaS, nginx for LBaaS.

Two design details worth stealing:

- **Generic interface naming.** The API spoke `ge0`, `ge1`, `ge2`; a driver
  layer mapped those to host device names. A legacy of the OpenBSD origin
  (`vio0`, `re0`) that turned out to be good decoupling.
- **Interfaces joined by MAC address.** The orchestrator called
  `GET /v1/system/interfaces`, got back `{ifname, lladdr}`, and joined against
  its own Neutron port list to build `network_id -> geN`.

Originally **OpenBSD + pf**; ported to Debian + iptables over three weeks in
August 2014 (`824f72612980` "Remove code to manage BSD Packet Filter").

### Ports and the data path

Astara maintained **two parallel sets of ports**:

|                    | Logical router ports             | Appliance ports           |
|--------------------|----------------------------------|---------------------------|
| Created by         | Neutron (`router-interface-add`) | the orchestrator          |
| `device_owner`     | `network:router_interface`       | **none**                  |
| `name`             | default                          | `ASTARA:VRRP:<router_id>` |
| `fixed_ips`        | the real router IPs              | `[]`                      |
| port security      | normal                           | **disabled**              |
| plugged into a VM? | **no**                           | yes                       |

Addresses came from the logical port; the interface name came from the
appliance port's MAC. The logical ports were never bound anywhere — pure IPAM
records. This is the same shape our trunk-less 1:1 design has, and the reason
`allowed_address_pairs` matters for us (see [§2.7](02-hooking-neutron.md)).

Hotplug was the weakest point. `nova interface-attach` is asynchronous with no
completion signal, so Astara polled the guest's own interface list for
`hotplug_timeout` (10s) and **destroyed and rebooted the VM** if it could not
confirm. Adding a subnet to a router could drop a tenant's traffic.

### Why it died

Commit `aeb93cc14dfb` in `openstack/governance`, Mark McClain, 2016-09-26:

> The Astara team believes the community's best interest are served by removing
> the Astara from TC governance. Most of the projects's core team has moved to
> different roles and there is not a ready set of maintainers available...

**People, not technology.** Akanda Inc. did not survive; the core team
dispersed. DreamCompute ran it in production for 1000+ customers and it beat
VMware NSX in a bake-off.

Technical pain points, each with a direct lesson for us:

1. Deep coupling to Neutron internals (→ use the service type framework).
2. Lying to Nova and Neutron (→ use real ports with proper ownership).
3. Boot latency on the API critical path (→ never block the API on a VM).
4. Unreliable, non-transactional hotplug (→ budget for it; reconcile, retry).
5. Resources scale with tenants, not traffic.
6. Unauthenticated REST with root-equivalent power.
7. Full-state push on every event, including every `port.create.end`.
8. Python 2, Debian jessie, dead toolchain.

What genuinely worked, and that we should preserve: one declarative full-state
document; a clean control/data-plane contract (the orchestrator knew nothing
about iptables, the appliance nothing about Neutron); the appliance as a
generic NFV container; per-resource state machines.

---

## Octavia

The reference for the VM-and-agent half. Still maintained, so its choices have
survived a decade of operational contact.

### Amphora agent API

Gunicorn + Flask on **9443**, `PATH_PREFIX = /1.0`, with **mandatory client
certificates**:

```python
options = {
    'bind': bind_ip_port,
    'workers': 1,
    'certfile': CONF.amphora_agent.agent_server_cert,
    'ca_certs':  CONF.amphora_agent.agent_server_ca,
    'cert_reqs': ssl.CERT_REQUIRED,      # <-- two-way TLS
}
```

**Two CAs, deliberately** — a server CA (Octavia issues one cert per amphora)
and a client CA (issues the controller's cert). The docs warn explicitly
against collapsing them: "a `server` certificate from an amphora could be used
to impersonate a controller."

The identity trick worth copying: **CN = amphora UUID**, pinned controller-side
regardless of the URL:

```python
class CustomHostNameCheckingAdapter(requests.adapters.HTTPAdapter):
    def cert_verify(self, conn, url, verify, cert):
        conn.assert_hostname = self.uuid    # the amphora UUID, not the IP
```

Strong per-instance identity with no DNS. Certs are Fernet-encrypted in the
Octavia DB, delivered via config drive at boot, and rotated by a housekeeping
loop through `PUT /1.0/certificate`.

### Interfaces by MAC, never by name

The controller tells the agent a **MAC address**; the agent resolves it:

```python
def _interface_by_mac(self, mac):
    with pyroute2.IPRoute() as ipr:
        idx = ipr.link_lookup(address=mac)[0]
    ...
    # Poke the kernel to re-enumerate the PCI bus.
    # We have had cases where nova hot plugs the interface but
    # the kernel doesn't get the memo.
    with os.fdopen(os.open('/sys/bus/pci/rescan', os.O_WRONLY), 'w') as f:
        f.write('1')
    raise ...  # 404, which the controller treats as retryable
```

That PCI rescan and the retryable 404 are the accumulated scar tissue of
hotplug races. Budget for the same.

### Declarative interface config

Octavia moved away from writing distro-native network config (`ifcfg-*`,
`/etc/network/interfaces`) to a **distro-neutral JSON file plus a dedicated
pyroute2 applier** (`amphora-interface`). Release note:

> Amphora network configuration for the VIP interface and the pool member
> interfaces are now applied with the amphora-interface tool. amphora-interface
> uses pyroute2 low-level functions to configure the interfaces instead of
> distribution-specific tools.

Addresses owned by keepalived are flagged `OCTAVIA_OWNED: False` so the applier
does not fight it. Directly relevant if we add FRR-managed routes later.

### Health and failover

HMAC-SHA256-signed, zlib-compressed JSON over **UDP** every 10s from amphora to
health-manager, round-robined across `controller_ip_port_list`. The health
manager fails over amphorae whose `last_update` is older than
`heartbeat_timeout` (60s), with a **circuit breaker** (`failover_threshold`) to
stop failover storms, and a flow that **builds the replacement before deleting
the original**:

> Do this early as the compute services have been observed to be unreliable.
> The community decided the chance that deleting first would open resources for
> an instance is less likely than the compute service failing to boot an
> instance for other reasons.

### lb-mgmt-net — the part everyone complains about

The controller must reach every amphora, which forces the control plane to join
a tenant-adjacent network. In devstack that is a tap device (`o-hm0`) on the
controller host bound to a Neutron port. In OpenShift, `octavia-operator` needs
a flat provider network attached to pods via Multus, a link router with
**`enable_snat: false`**, host routes in both directions, and a reserved pool of
**predictable controller IPs** because `controller_ip_port_list` is baked into
each amphora at boot.

**This is the single most criticised element of the design, and the main reason
our config channel uses vendordata instead** ([§3.3](03-configuration.md)). Lab
experiment 06 showed our status channel needs far less than this — a host route
and a security group.

### Provider driver framework vs Neutron's

Octavia's `octavia-lib` is the lesson learned from Neutron's own history:

|                        | Neutron service type / flavors               | Octavia provider drivers                                    |
|------------------------|----------------------------------------------|-------------------------------------------------------------|
| Driver ABI             | imports Neutron internals; **no stable ABI** | **versioned, narrow, documented** in a separate library     |
| Execution              | in-process, DB session access                | out-of-process friendly, asynchronous                       |
| Capability negotiation | ad hoc                                       | explicit (`NotImplementedError` / `UnsupportedOptionError`) |
| Status reporting       | mutate the model                             | explicit push API via the driver agent                      |

The takeaway for our upstream strategy: **the driver boundary should be a
separately-released library with a documented stable surface**, and status
should flow back through an explicit API rather than direct model mutation.

### Documented pain points

Boot latency (5 min waiting for ACTIVE, 10 more for the agent); one VM per load
balancer (two for HA, anti-affinity forced); image rotation requires failing
over every amphora; the control plane must stay backward compatible with
in-field agents forever ("every byte of the agent API is a multi-release
compatibility commitment"); certificate rotation with harsh failure modes; and
the management network complexity above.

---

## What we take from each

| Concern                     | Source                                | Decision                                                |
|-----------------------------|---------------------------------------|---------------------------------------------------------|
| Neutron integration         | **neither** — flavors postdate Astara | Service type framework + router flavor                  |
| Config document shape       | Astara                                | One idempotent, declarative, full-state document        |
| Config transport            | **neither**                           | Nova dynamic vendordata (no mgmt net, boot-independent) |
| Interface identification    | both                                  | By MAC, never by kernel name                            |
| Agent API auth              | Octavia                               | mTLS, CN = router UUID, pinned                          |
| Credential rotation         | Octavia's problem, our fix            | Rotate through vendordata, which is live                |
| In-guest config application | Octavia                               | Distro-neutral JSON + pyroute2, not distro files        |
| Health                      | Octavia inverted                      | Controller-initiated poll carrying desired generation   |
| Failover                    | Octavia                               | Build before destroy; circuit breaker                   |
| Hotplug                     | Astara's worst subsystem              | Reconcile and retry; never reboot on timeout            |
| Management network          | Octavia's worst subsystem             | Needed for status only; a route + a security group      |
