<sub>[Index](intro.md) · [&larr; First draft](04-first-draft.md) · [Performance &rarr;](06-performance.md)</sub>

# 5. Lifecycle

## 5.1 What exists to reuse, and how large a daemon we need

| Available | Useful for |
|---|---|
| `neutron_worker.PeriodicWorker` | Running a loop inside `neutron-server` |
| Registry callbacks | Change notification ([§2.3](02-hooking-neutron.md#23-events-we-receive)) |
| `service_base.load_drivers()` | Driver loading |
| DB session, alembic branches | Persistence ([§3.2](03-configuration.md#32-persisted-state)) |
| `ProviderResourceAssociation` | Knowing which routers are ours |

Not available: any VM lifecycle machinery, job/task framework, retry or backoff
infrastructure, leader election, state machine helper, or any notion of a
resource that takes minutes to become real. Neutron's L3 model assumes an agent
that already exists on a host; ours has to be created.

Octavia runs three services — `controller_worker` (taskflow), `housekeeping`
(spare pool, cert rotation, DB cleanup) and `health_manager` (UDP listener plus
failover). Much of that exists for reasons we do not share: active/standby
pairs, a spare pool to hide boot latency, config-drive cert rotation, failover
as a first-class operation.

**Proposal: one reconciler process, level-triggered, no taskflow.**

```
for each router owned by us:
    desired = load_document(router)
    actual  = inspect(nova, neutron)
    converge(desired, actual)

PENDING -> BOOTING -> ATTACHING -> ACTIVE -> DELETING -> (gone)
                \          \          /
                 +-> ERROR <---------+
```

Level-triggered because it is restartable by construction (a crash
mid-operation is indistinguishable from a slow operation), cannot drift by
missing an event (Astara's failure mode was full-state push on every event,
including every `port.create.end`), and needs no job board or compensating
transactions. The cost is that everything must be expressible as "compare and
fix" — the same idempotence the document already demands. Taskflow can come
later if some operation genuinely needs ordered compensation.

## 5.2 Failure handling and currently undefined behaviour

| Failure | Response |
|---|---|
| Nova boot fails | Bounded retry with backoff, then ERROR with a recorded reason |
| Hotplug does not complete | Retry ([`Q6`](open-questions.md)). **Do not reboot the VM** — Astara's worst behaviour; adding a subnet could drop a tenant's traffic |
| VM deleted out of band | Rebuild. The document is unchanged, so the new VM converges to the same state |
| Compute host fails | Same path. Evacuate versus rebuild is [`Q16`](open-questions.md) |
| Agent unreachable | `unknown`, not ERROR. Take no destructive action |
| Manager down | Nothing happens: routers keep forwarding, new ones queue. The correct failure mode, and worth testing explicitly |

Two rules from Octavia, both learned the hard way: **build before destroy**
(compute services are unreliable, and deleting first to free resources is a
worse bet than the replacement simply failing to boot), and a **circuit
breaker** — if fifty routers go `unknown` at once, the problem is the network,
not fifty VMs.

Three things nobody has defined yet:

- **`admin_state_up = False`.** Candidates: shut the VM down, stop forwarding
  but keep it, or ignore. **Recommend stop forwarding, keep the VM** — it
  matches the reference implementation's observable behaviour and is instantly
  reversible. Needs to be in the document so the agent can act on it ([`Q20`](open-questions.md)).
- **Garbage collection.** The reverse sweep must exist too: VMs with no router,
  ports with no VM, mgmt ports from failed boots, documents with no router.
  Deletion order is detach, delete VM, delete our ports, and a half-completed
  delete must be resumable — which it is, if the sweep is level-triggered like
  everything else. Tag every resource with the router ID so an orphan is
  identifiable without a DB lookup; the PoC's `router-vm-<name>` convention had
  the right instinct and the wrong key. **Force delete** must never block on
  the appliance: worst case the VM becomes an orphan and the sweep collects it.
- **Live migration.** Appliances are ordinary instances, so operators will
  migrate them. **To verify** whether live migration preserves conntrack and
  (stage 2) IPsec SAs — the state is in the guest so it should survive, but the
  pause and the post-move ARP need measuring. Cold migration and evacuation
  definitely lose it ([`Q21`](open-questions.md)).

## 5.3 Image and upgrade

Octavia documents that rotating the amphora image requires failing over every
amphora; we inherit the same property, since a new image only takes effect on
rebuild. Needs image provenance in RHOSO (OSPRH-40373), a version pinned per
flavor via `metainfo` ([§2.8](02-hooking-neutron.md#28-vm-sizing-via-metainfo)), and an upgrade story that is not "rebuild every
router in the cloud at once" ([`Q12`](open-questions.md)). The control plane must also stay
compatible with in-field agents — `schema_version`, [§3.1](03-configuration.md#31-the-document-and-the-generation-number).

## 5.4 HA: VRRP versus BFD

Stage 3, described here only so stage 0 does not foreclose it.

| | VRRP (keepalived) | BGP/BFD (FRR) |
|---|---|---|
| Model | Active/standby, VIP per interface, gratuitous ARP on failover | Both active, routes advertised, BFD detection |
| Detection | ~1-3 s typical | sub-second |
| Active/active | no | yes |
| Needs `allowed_address_pairs` | yes | no |
| Needs L2 adjacency on every network | yes | no |
| Needs an upstream BGP speaker | no | yes |
| Stateful NAT survives failover | with conntrackd | no |
| **IPsec survives failover** | only with SA sync | no |

Prior art for each: Astara, Octavia and Neutron's own L3-HA routers all use
VRRP; Octavia flags keepalived-owned addresses `OCTAVIA_OWNED: False` so its
applier does not fight it, and we would need the same discipline. For BGP, OVN
logical routers support BFD on static routes and ECMP next-hops, so a transit
network with an OVN router doing ECMP across two appliances is plausible
(**to verify** against the RHOSO OVN version); `neutron-dynamic-routing` is the
other candidate.

**The IPsec row decides it.** ESP SAs are per-peer state with replay counters,
so ECMP across two independent appliances breaks a tunnel unless exactly one
terminates it, and libreswan has no SA synchronisation (strongSwan's HA plugin
needs a kernel patch and is not a realistic dependency). So plain routing could
go active/active with BGP/BFD, but **IPsec realistically means active/standby**
with tunnels re-established on failover. Since IPsec is the end goal, VRRP is
the likelier answer, with BGP/BFD as an optimisation for routing-only flavors.
A direction, not a decision. OSPRH-40375.

## 5.5 Scale and manager availability

"Does one reconciler keep up" needs defining before it can be answered:

| Dimension | Question |
|---|---|
| Routers per manager | At what count does a full pass stop fitting in its interval ([§6.4](06-performance.md#64-control-plane-and-scale-benchmarks) C4) |
| Cold start | Starting against 5000 routers must not stampede Nova. Rate limiting and randomised ordering from the first version |
| Status rate | N routers × interval. Shape (ii) makes this inbound, which is easier to scale |
| Burst create | A heat stack creating 50 routers at once; Nova boot concurrency must be bounded or we become the noisy neighbour |
| Jitter | Every periodic action needs it, or N agents installed together poll in lockstep forever. The PoC's jittered initial delay was the right instinct |

A level-triggered reconciler answers most of this naturally: cap the work per
pass and let the next one continue. Convergence degrades under load instead of
failing.

The manager's own availability is [`Q18`](open-questions.md): a single restarted instance is
acceptable (while it is down, existing routers keep forwarding and new ones
wait), active/passive needs only a DB row with a TTL, and sharding by router ID
hash is only warranted if one process cannot keep up.
