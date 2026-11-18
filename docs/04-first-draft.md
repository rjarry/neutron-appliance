<sub>[Index](intro.md) · [&larr; Configuration and state](03-configuration.md) · [Lifecycle &rarr;](05-lifecycle.md)</sub>

# 4. First draft: static IPv4 routing on the Linux stack

The smallest thing that is end-to-end real.

## 4.1 Scope

| In | Neutron trigger |
|---|---|
| Create router, boot VM | `ROUTER AFTER_CREATE` with the appliance flavor |
| Delete router, delete VM and ports | `ROUTER AFTER_DELETE` |
| Add/remove internal interface | `ROUTER_INTERFACE AFTER_CREATE` / `AFTER_DELETE` |
| External gateway, default route | `external_gateway_info` |
| Extra routes | `ROUTER PRECOMMIT_UPDATE` |
| Report converged / converging / unknown | [§3.4](03-configuration.md#34-status-channel-health-and-the-management-network) |

Out: IPv6, SNAT, floating IPs, port forwarding, IPsec, HA, trunk ports,
provider migration, multiple external gateways, `enable_ndp_proxy`, BFD/ECMP
attributes — all **rejected explicitly** ([§2.2](02-hooking-neutron.md#22-the-api-surface-and-rejecting-what-we-cannot-do)).

Excluding SNAT means stage 0 routers give tenant VMs no outbound internet.
Fine for a functional milestone, not to be mistaken for a usable product.

## 4.2 Neutron to Linux

| Document element | Linux |
|---|---|
| `interfaces[].mac` | `ip link` lookup by address, rename to a stable name |
| `interfaces[].addresses` | `ip addr add` |
| `interfaces[].mtu` | `ip link set mtu` — must match the Neutron network MTU or Geneve fragments |
| `routes[]`, default route | `ip route replace <dst> via <nh>` |
| forwarding | `net.ipv4.ip_forward = 1` |
| anti-spoof | `rp_filter` **0 or 2**, never 1 — asymmetric paths through a router are normal |
| `admin_state_up: false` | Stop forwarding, keep the VM ([§5.2](05-lifecycle.md#52-failure-handling-and-currently-undefined-behaviour)) |

Four things not in any table that cause support cases:

- **ICMP must not be filtered.** Dropping `time-exceeded` breaks traceroute;
  dropping `fragmentation-needed` breaks PMTUD and produces the classic "small
  requests work, large ones hang". Needs an explicit test.
- **TCP MSS clamping.** Geneve already reduces tenant MTU and IPsec reduces it
  further; clamping forwarded SYNs is effectively mandatory
  (`tcp option maxseg size set rt mtu`). Build it in at stage 0 rather than
  discover it at stage 2.
- **Gratuitous ARP / unsolicited NA** on address assignment. Free now,
  load-bearing in stage 3 failover.
- **Neighbour table sizing** from the attached subnet sizes, or `gc_thresh`
  eviction under load.

## 4.3 The agent

```
# guest -> metadata, every 15-30 s
GET http://169.254.169.254/openstack/latest/vendor_data2.json
  -> {"static": {}, "router": { <document> }}      # unwrap ["router"]

# status, shape per [§3.4](03-configuration.md#34-status-channel-health-and-the-management-network)
{"router_id": "...", "applied_generation": 41,
 "state": "converging", "errors": ["interface fa:16:3e:.. absent after 90s"]}
```

Reconcile loop: fetch, unwrap, ignore if not newer, diff actual against
desired, apply, record applied generation. **Never advance the generation on
partial success.** An interface whose MAC has not appeared is not an error, it
is "not converged yet". `errors[]` carries human-readable strings, not codes —
until [`Q17`](open-questions.md) is answered it is the only diagnostic channel out of the VM.

Implementation choices, from prior art:

- **Apply with pyroute2, not distro network config files.** Octavia migrated
  from `ifcfg-*` to a distro-neutral JSON file plus a pyroute2 applier exactly
  so the agent is not distro-specific. Shelling out to `ip` is acceptable at
  first, but the internal interface should not assume it.
- **Keep the management NIC out of the tenant routing domain** so a tenant
  default route cannot hijack the management path. The PoC used a netns; a VRF
  or policy routing does the same. Cheap to defer until [`Q4`](open-questions.md) settles — under
  shape (ii) the mgmt path only needs to survive outbound.
- **systemd `Restart=always` with backoff.** A crash must not leave a router
  unmanaged, and a crash loop must not hammer the metadata service.
- **The agent is not load-bearing for forwarding.** Once the kernel is
  configured, packets flow whether it lives or dies: an agent failure means
  "configuration frozen", not "traffic down". So a watchdog that reboots the VM
  on agent failure would be actively harmful.
- **The agent owns its configuration exclusively** — anything it did not put
  there, it removes. That is what makes the reconcile model true rather than
  aspirational.
- **Version skew direction is fixed**: a new manager must work with an old
  agent, because the manager upgrades first and agents only change on rebuild.

## 4.4 Done means

1. `router create --flavor-id <appliance>` returns immediately; a VM appears in
   the service project within a bounded time.
2. Two tenant networks attached, a VM on each, and they ping through the
   appliance.
3. `router set --route ...` shows up in `ip route` inside the appliance.
4. Hard-reboot the appliance: it reconverges with no control-plane action.
5. Stop the manager, reboot the VM: it still reconverges (boot independence),
   and status reports `unknown`, not ERROR.
6. `router delete` removes the VM and leaves no orphan ports.
7. Delete the VM behind Neutron's back: the manager rebuilds and the router
   recovers with no user action.
8. An unsupported option is rejected with a clear error, not ignored.
9. An appliance router and a default OVN router coexist, both working.
10. A functional CI job covers the above. There is currently **no functional or
    tempest coverage of the router-flavor path anywhere in the Neutron tree and
    no Zuul job exercising it** — unit tests only (**code-read**):
    `tests/unit/services/ovn_l3/service_providers/test_user_defined.py`,
    `.../test_driver_controller.py`,
    `tests/unit/services/l3_router/service_providers/test_driver_controller.py`
    and `tests/unit/extensions/test_flavors.py`. So the job has upstream value
    on its own (OSPRH-40385), and it is what will catch Neutron breaking us,
    which is how Astara died.

    In-tree documentation is equally thin:
    `doc/source/admin/config-router-flavor-ovn.rst` is the only end-to-end
    guide, `doc/source/contributor/new_extensions.rst:508-528` covers provider
    registration, and **there is no devref on writing an L3 service provider
    driver at all**.
