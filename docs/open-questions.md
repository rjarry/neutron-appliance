<sub>[Index](intro.md) · [&larr; Performance](06-performance.md)</sub>

# Open questions

Every unresolved decision in the design, in one place. Referenced as `Q1`..`Q24`
throughout the other chapters. Closed items are struck through rather than
deleted, so that a question is never silently dropped.

Resolving `Q1` unblocks the most: it decides where the reconciler and the
vendordata endpoint live, which in turn settles `Q2` and `Q18`.

| # | Question | Blocks |
|---|---|---|
| ~~Q1~~ | ~~Reconciler in `neutron-server` or a separate service~~ **Decided:** a `PeriodicWorker` inside `neutron-server`. Revisit if it hurts | — |
| Q2 | Where state is persisted, and whose database ([§3.2](03-configuration.md#32-persisted-state)) | Stage 0 |
| ~~Q4~~ | ~~Status channel: controller- or agent-initiated~~ **Decided:** the agent pulls config and reports status in one request ([protocol.md](protocol.md)) | — |
| Q5 | Keep the inert phantom LSP or copy the delete/recreate dance ([§2.7](02-hooking-neutron.md#27-port-attachment)) | Stage 0 |
| Q6 | Hotplug retry limit, and what happens when exceeded ([§2.7](02-hooking-neutron.md#27-port-attachment)) | Stage 0 |
| Q7 | Boot ordering — the document may name MACs Nova has not attached yet. Needs a policy and a test | Stage 0 |
| Q8 | Where convergence state is surfaced, given no router `status` write path ([§3.4](03-configuration.md#34-status-channel-health-and-the-management-network)) | Stage 0 usability |
| ~~Q9~~ | ~~`nova.conf` / nova-operator coupling~~ **Eliminated** by dropping vendordata | — |
| Q10 | Are PSKs acceptable in the vendordata document, or do they need separate retrieval ([§3.5](03-configuration.md#35-tls-and-secrets)) | Stage 2 |
| Q11 | PKI ownership, CA structure, key storage ([§3.5](03-configuration.md#35-tls-and-secrets)) | Stage 0 under shape (i), stage 2 regardless |
| Q12 | Image provenance and upgrade story in RHOSO ([§5.3](05-lifecycle.md#53-image-and-upgrade)) | Stage 0 delivery |
| Q13 | IPv6 throughout. Neutron routers are dual-stack; the PoC was IPv4-only | Stage 1 |
| Q14 | Upstream vehicle: in-tree, separate repo, or a versioned driver library as `octavia-lib` is for Octavia | Stage 0 packaging |
| Q15 | `requires_lsp` upstream proposal, and contact with the LP#2078382 operator who wanted the same thing | Upstream strategy |
| Q16 | VM failure policy: rebuild versus evacuate on compute host loss ([§5.2](05-lifecycle.md#52-failure-handling-and-currently-undefined-behaviour)) | Stage 0 robustness |
| Q17 | How to get logs and diagnostics out of the appliance ([§3.6](03-configuration.md#36-observability-and-operator-access)) | Stage 0 supportability |
| Q18 | Manager availability: single, active/passive, or sharded ([§5.5](05-lifecycle.md#55-scale-and-manager-availability)) | Stage 0 deployment |
| Q19 | Can the flavor be restricted to specific projects, and what is the quota story ([§2.5](02-hooking-neutron.md#25-service-project-quota-and-the-cost-model)) | Deployment policy, agree early |
| Q20 | `admin_state_up = False` semantics ([§5.2](05-lifecycle.md#52-failure-handling-and-currently-undefined-behaviour)) | Stage 0 |
| Q21 | Does live migration preserve conntrack and IPsec SAs ([§5.2](05-lifecycle.md#52-failure-handling-and-currently-undefined-behaviour)) | Operations, stage 2 |
| Q22 | Does ESP offload engage over virtio in a guest ([§6.2](06-performance.md#62-expectations-before-measuring)) | Stage 2 performance |
| Q23 | Does RHOSO enable the `segments` service plugin? It is the only component that advertises `ip_allocation`, which is what decides whether ipless ports can be attached at boot instead of only by hotplug ([§2.7](02-hooking-neutron.md#27-port-attachment)) | Nothing — hot-attach already works. Would simplify boot if true |
| Q24 | Port security off versus `allowed_address_pairs` on the appliance's attachment ports, before stage 1 adds floating IPs ([§2.9](02-hooking-neutron.md#29-port-ownership-the-project_id-problem)) | Stage 1 |

## Closed

| # | Question | Resolution |
|---|---|---|
| Q3 | Port ownership: can we attach ports the appliance's project does not own? | **Closed 2026-11-16** by [experiment 08](experiments.md#08--port-ownership). Proposal A works: leave Neutron's port as a pure IPAM record and attach our own ipless port in the service project |

## Also closed earlier, by the first lab round

| Question | Resolution |
|---|---|
| Does Nova dynamic vendordata work as a live config channel? | **Yes** — per-request, no caching, 1 s propagation ([experiment 01](experiments.md#01--dynamic-vendordata-is-a-live-per-request-channel)) |
| Which management network model? | Option (b), a tenant network behind a default-flavour OVN router ([experiment 06](experiments.md#06--management-network-option-b)) |
| Is service-profile `metainfo` usable for VM sizing? | **Yes**, mechanically ([experiment 04](experiments.md#04--metainfo-for-per-flavor-sizing)) |
| Does the phantom OVN LSP break traffic? | **No**, it is inert ([experiment 03](experiments.md#03--the-phantom-logical_switch_port)) |
| How large is the vendordata blast radius? | Narrow — only `vendor_data2.json` requests ([experiment 07](experiments.md#07--vendordata-blast-radius)) |

## Decided during planning

| # | Question | Decision |
|---|---|---|
| Q-A | How does the agent authenticate to the sync endpoint? | Shared Keystone service user for admission, per-router token for authorisation ([protocol.md](protocol.md) §2). No `api-paste.ini` change |
| Q-B | Bootstrap via config drive or user data? | **Config drive** — no metadata service, no network needed |
| Q-C | One distribution, or separate server and agent packages? | **One** for now, with the boundary held by ruff banned-api and an import test |
| Q-D | May the agent depend on oslo and keystoneauth1? | **Yes.** Anything that drags in Neutron, no |
| Q-E | How is the appliance image built? | **diskimage-builder**, RHEL 9 / CentOS Stream 9 base |
| Q-F | Upstream from day one? | Upstream-shaped regardless of where it is hosted |
| Q-G | One shared management network, or one per router? | **One shared**, in the service project |
| Q-H | Target Python and Neutron versions? | **Python 3.9** (RHOSO RHEL 9 baseline). Exact Neutron version still **to verify** |
