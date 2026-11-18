# Neutron router appliance, towards IPsec VPNaaS

Status: draft for discussion.

## What this is

A design for an alternative Neutron L3 router implementation: each router with
a given flavor is backed by **one dedicated Nova VM** running a Linux data
plane, plugged into Neutron through the **Service Type Framework** rather than
by patching Neutron itself.

Plain routing is not the goal. The goal is a place to terminate **IPsec with
libreswan**, which ML2/OVN has nowhere to run today. The router appliance is
the vehicle; IPsec is the cargo. A DPDK data plane (grout) is a possible far
future, deliberately kept behind an abstraction rather than designed for now.

**This repository holds the implementation.** The design research, the lab and
the eight experiments behind it live in `neutron-stf-poc`.

Tracked as [OSPRH-40195](https://redhat.atlassian.net/browse/OSPRH-40195),
child of [RHOSSTRAT-1487](https://redhat.atlassian.net/browse/RHOSSTRAT-1487)
("STF: VPN As a Service").

## Read in this order

| Document | Contents |
|---|---|
| [01-goal.md](01-goal.md) | What STF is, why a service VM, alternatives rejected, staging and non-goals |
| [02-hooking-neutron.md](02-hooking-neutron.md) | Registration, the API surface, events, sync versus async, who boots the VM, port ownership |
| [03-configuration.md](03-configuration.md) | The desired-state document, generation numbers, how config is delivered, status, TLS, observability |
| [protocol.md](protocol.md) | The normative server/agent contract |
| [04-first-draft.md](04-first-draft.md) | Stage 0 scope: static IPv4 routing on the Linux stack |
| [05-lifecycle.md](05-lifecycle.md) | Reconciler design, failure handling, HA, control-plane scale |
| [06-performance.md](06-performance.md) | Targets, benchmark matrices, method |
| [open-questions.md](open-questions.md) | Every unresolved decision, numbered `Q1`..`Q24` |

Supporting material:

| Document | Contents |
|---|---|
| [findings.md](findings.md) | What the lab measured, and which assumptions were wrong |
| [experiments.md](experiments.md) | One section per experiment: question, method, result |
| [prior-art.md](prior-art.md) | Astara and Octavia, analysed in detail |
| [plans/stage-pre-0.md](plans/stage-pre-0.md) | The current implementation plan |

## Terminology

Used consistently throughout. Earlier documents in `archive/` are looser.

| Term | Meaning |
|---|---|
| **appliance** | The Nova VM backing one Neutron router |
| **agent** | The process inside the appliance that applies configuration |
| **manager** | The control-plane process that boots VMs, attaches ports and reconciles |
| **driver** | The `L3ServiceProvider` subclass loaded inside `neutron-server` |
| **document** | The desired-state JSON for one router |
| **generation** | Monotonic integer version of the document |
| **service project** | The Keystone project that owns appliance VMs |
| **stage 0..4** | Delivery stages, from static IPv4 routing to a DPDK data plane |
| **ipless port** | A Neutron port with `fixed_ips: []` — a MAC and a wire, with no address allocated and nothing reserved in IPAM. Created with `--no-fixed-ip`; recorded internally as `ip_allocation = 'none'`. The appliance's data NICs are these |
| **IPAM record** | The `network:router_interface` or `network:router_gateway` port Neutron creates. It holds the address and is attached to nothing. Its address is configured onto an ipless port inside the guest |

**"Attach" means two different things** and the distinction is load-bearing:

| | Neutron sense | Nova sense |
|---|---|---|
| Operation | `add_router_interface` | `interface_attach` |
| Effect | Registers a port as a logical router interface | Hot-plugs a vNIC into a VM |
| Port needs a fixed IP? | **Yes, mandatory** | No, on hot-attach |

Both happen in this design, to **different ports**. Unqualified "attach" in
these documents means the Nova sense. See
[§2.7](02-hooking-neutron.md#27-port-attachment).

## A disclaimer about claims

Software design documents rot because nobody can tell which sentences were
verified and which were plausible at the time of writing. Every factual claim
here is therefore tagged:

| Tag | Meaning |
|---|---|
| **measured** | Observed in the lab. The run is written up in [experiments.md](experiments.md) and the conclusion in [findings.md](findings.md) |
| **code-read** | Read directly in the Neutron or Nova source, with a `file:line` reference |
| **assumed** | Reasoned from experience, not verified. Treat as a hypothesis |
| **to verify** | Known gap. Someone has to go and check |

Untagged statements are design intent — decisions and opinions, not facts.

This matters more than it sounds. Several claims in earlier drafts were wrong
and were only caught by measurement: that `metadata_cache_expiration` would
bound convergence latency, that the phantom OVN LSP blackholed traffic, that
the vendordata endpoint would sit in the hot path of every metadata request,
and that the `project_id` problem affected only external gateway ports. All
four are recorded in the corrections table in [findings.md](findings.md),
along with the two experiments that produced a confident false negative before
the confounder was found.

If you add a claim, tag it. If you verify an **assumed** one, promote it and
say where.
