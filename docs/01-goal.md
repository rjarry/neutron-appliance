<sub>[Index](intro.md) · [&larr; Introduction](intro.md) · [Hooking into Neutron &rarr;](02-hooking-neutron.md)</sub>

# 1. Goal

## 1.1 The Service Type Framework

STF lets a service type have more than one implementation, chosen per resource
instead of per deployment:

```ini
# neutron.conf
[DEFAULT]
service_plugins = ovn-router,flavors,...         # 'flavors' is mandatory
[service_providers]
# <service_type>:<provider_name>:<driver_path>[:default]
service_provider = L3_ROUTER_NAT:appliance:my_pkg.drivers.ApplianceL3Driver
#                  ^service type ^provider ^driver path
```

```bash
# the profile carries the driver path - the only thing that actually links
# the flavor back to the config line above
openstack network flavor profile create --enable \
    --driver my_pkg.drivers.ApplianceL3Driver

# 'appliance-router' is just a user-facing label, unrelated to 'appliance'
openstack network flavor create --service-type L3_ROUTER_NAT appliance-router

openstack network flavor add profile <flavor-id> <profile-id>
openstack router create --flavor-id <flavor-id> r1
```

**Three names, and only one of them is a join key.** This is the most
confusing part of the framework:

| Name | Defined in | Visible where |
|---|---|---|
| `appliance` | provider name, 2nd field of `service_provider` | Internal only — `providerresourceassociations.provider_name`. **Never appears in the flavor API** |
| `my_pkg.drivers.ApplianceL3Driver` | driver path, 3rd field, and the profile's `--driver` | The join key, matched by exact string equality |
| `appliance-router` | flavor name, arbitrary | What tenants see in `openstack network flavor list` |

The flavor name is nowhere in the resolution chain. The driver path is the
only thing joining the two halves:

```
  neutron.conf                          flavor profile
  ------------                          --------------
  L3_ROUTER_NAT : appliance : PATH      --driver PATH
                  ^^^^^^^^^   ^^^^               ^^^^
                  provider      \                 /
                      |          \  exact match  /
                      |           +------+------+
                      |                  |
                      v                  v
           drivers['appliance']  <--  get_flavor_next_provider()
                                      filters={'driver': PATH} -> provider name
```

So the flavor could be called anything; but a single character of difference
between the config line and `--driver` makes router creation fail, and that is
the first thing everyone hits (`neutron/db/flavors_db.py:217-251`,
**code-read**). Provider names are lowercased and capped at 255 chars, and
driver paths must not contain `:` because the config line is colon-delimited.

No stevedore entry point is needed. Neutron tries to resolve the driver string
as a stevedore alias but **falls back to the literal dotted path**
(`provider_configuration.py:135-156`), which `load_drivers()` then instantiates
with `importutils.import_object()`. The package only has to be importable by
`neutron-server` — no `setup.cfg` plumbing, despite what most OpenStack plugin
documentation implies.

A router with that flavor is dispatched to our driver; every other router stays
with the OVN L3 plugin, in the same deployment.

This is why STF is the right hook: supported, non-invasive, per-router opt-in.
Astara predates flavors, subclassed the core and L3 plugins instead, and every
Neutron release broke it. Not repeating that is the single most important
inheritance from prior art.

## 1.2 What we are building, and why

Each router with the appliance flavor is backed by **one dedicated Nova VM**.
Neutron stays the IP address manager and owner of desired state; an agent in
the VM applies it to a Linux data plane. Those packets never enter the OVN
pipeline.

Plain routing is not the point — **a place to terminate IPsec with libreswan**
is. `neutron-vpnaas` runs libreswan or strongSwan inside the L3 agent's router
namespace (`neutron_vpnaas.services.vpn.device_drivers.libreswan_ipsec`). On
ML2/OVN there is no such namespace, and no OVN-native equivalent in-tree
(**to verify** before the upstream conversation). So there is nowhere to run
`pluto`. A service VM is that somewhere.

Alternatives, asked often enough to answer here:

| Alternative | Why not |
|---|---|
| **OVN IPsec** (`ovn-ipsec`) | Different problem: it encrypts Geneve tunnels *between chassis*. It does not terminate tenant VPNs and is invisible to the Neutron VPN API. The name collision causes real confusion |
| **A VPN agent on network nodes** | Reintroduces the centralised network node OVN removed; one shared kernel and failure domain for all tenants' IPsec |
| **Container instead of a VM** | Needs a runtime on the data path, Multus-style attachments, a different tenancy boundary. A Nova VM is the isolation unit OpenStack already has, and Nova already attaches Neutron ports to it |
| **Hardware / third-party appliance** | Legitimate and orthogonal — STF is exactly the hook such a driver would use. Stay generic enough not to preclude one |
| **Extending OVN itself** | Out of our control, multi-year, does not help existing deployments |

The driver should therefore be a **generic appliance provider**, not a
grout- or libreswan-specific one; the data plane lives behind the agent's
backend abstraction ([§6.6](06-performance.md#66-when-grout-becomes-relevant)). A provider tied to one vendor's data plane is a
much harder upstream sell.

Tracked as [OSPRH-40195](https://redhat.atlassian.net/browse/OSPRH-40195),
child of [RHOSSTRAT-1487](https://redhat.atlassian.net/browse/RHOSSTRAT-1487)
("STF: VPN As a Service").

## 1.3 Staging and non-goals

| Stage | Scope | Data plane |
|---|---|---|
| 0 | Static IPv4 routing, 1 router = 1 VM | Linux kernel, `ip` / `nft` |
| 1 | SNAT, floating IPs, IPv6 | Linux kernel |
| 2 | IPsec site-to-site | libreswan (IKE) + kernel XFRM (ESP) |
| 3 | HA | VRRP or BGP/BFD ([§5.4](05-lifecycle.md#54-ha-vrrp-versus-bfd)) |
| 4 | High throughput, opt-in | grout (DPDK) |

Stage 0 is specified in §4; later stages appear only where they constrain it.
Each stage must be independently shippable and independently abandonable.

Note for stage 2: `pluto` only speaks IKE — the data path is the kernel's XFRM
stack, so "libreswan performance" means "XFRM performance" and stage 2 is
config generation plus daemon supervision, not forwarding. Stage 4 changes
that: a DPDK data plane has to own ESP itself.

Deliberately out of scope for now:

| Non-goal | Why | Revisit |
|---|---|---|
| More than one router per VM | Removes the scheduler, VRF multiplexing and most of the sizing question | If VM count becomes the cost driver |
| Trunk ports / VLAN subports | One vNIC per interface, hot-attached | If vNIC count becomes a real limit ([§2.7](02-hooking-neutron.md#27-port-attachment)), or at stage 4 ([§6.6](06-performance.md#66-when-grout-becomes-relevant)) |
| HA | The single VM must work first | Stage 3 |
| Provider migration | `flavor_id` is create-only; `_update_router_provider()` raises `NotImplementedError` (**code-read**) | Needs an upstream spec |
