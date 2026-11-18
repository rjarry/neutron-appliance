<sub>[Index](intro.md) · [&larr; Lifecycle](05-lifecycle.md) · [Open questions &rarr;](open-questions.md)</sub>

# 6. Performance

## 6.1 Target

The epic states **1-5 Gbps**, which is not a specification. These must be fixed
before any number can pass or fail:

| Variable | Why |
|---|---|
| Packet size | 5 Gbps at 1500 B is ~410 kpps; at IMIX (~350 B) it is ~1.8 Mpps — over 4x the work for the same headline |
| Direction | North-south through the gateway versus east-west between tenant networks |
| Flow count | A single flow cannot use RSS; it pins to one queue and one core |
| Features | Plain forwarding, SNAT, 1:1 NAT and IPsec are four cost profiles |
| Cipher | AES-GCM-128 with AES-NI is several times faster than AES-CBC + HMAC-SHA256 |
| Core count | Per-core throughput is the portable number; total is the saleable one |
| Loss tolerance | Zero-loss (RFC 2544) and "best effort iperf3" differ by a lot |

**Proposed target: 5 Gbps bidirectional at IMIX, zero loss, 4 vCPU, plain
forwarding; 1 Gbps for IPsec with AES-GCM-128 on the same VM.** A starting
position to be argued with, not a commitment.

## 6.2 Expectations before measuring

**Assumed** — this is exactly what the benchmarks exist to replace:

| Configuration | Expectation |
|---|---|
| Kernel forwarding, virtio + vhost-net, 1 core | ~1-2 Mpps |
| Kernel forwarding + nftables SNAT | 20-40% lower, conntrack-bound |
| XFRM ESP, AES-GCM-128, AES-NI, 1 core | ~1-3 Gbps |
| XFRM ESP, AES-CBC + SHA256, 1 core | roughly half of AES-GCM |
| grout / DPDK (stage 4) | 5-10x the kernel at small packets |

Two structural points: **IPsec defeats GRO/GSO** unless ESP offload is in play
(`esp4_offload` exists; whether it engages over virtio in a guest is [`Q22`](open-questions.md), and
it is probably the largest single lever for stage 2); and **the appliance is
not the only hop** — traffic also crosses the compute node's Geneve
encapsulation, so an end-to-end number includes OVN's cost and an
appliance-only number does not. Measure both, and never conflate them.

## 6.3 Data-plane benchmarks

| # | Case | Traffic | Measures |
|---|---|---|---|
| B1 | Baseline: OVN distributed router, no VM | IMIX + 64 B + 1500 B | The ceiling; everything else is a percentage of it |
| B2 | Appliance, plain forwarding, east-west | same | Cost of the VM hop |
| B3 | Appliance, north-south via gateway | same | Adds external network and Geneve |
| B4 | B2 + SNAT | same, many flows | Conntrack cost and table scaling |
| B5 | B2 + floating IP 1:1 NAT | same | Stateless NAT cost |
| B6 | IPsec site-to-site, AES-GCM-128 | 1400 B + IMIX | Stage 2 headline |
| B7 | IPsec, AES-CBC + SHA256 | same | Cipher sensitivity |
| B8 | B6 with ESP offload on/off | same | Is the offload lever real |
| B9 | B6 scaled to N tunnels | same | Per-SA overhead, `pluto` under churn |
| B10 | Core scaling 1/2/4/8 vCPU on B2 and B6 | IMIX | Linearity; informs `metainfo` sizing |
| B11 | Reference: L3 agent namespace router (ML2/OVS) | IMIX | Answers "is a VM worse than a namespace", which will be asked |

Record per run: throughput (Gbps and pps), loss, latency p50/p99/p99.9, CPU per
Gbps, per-queue distribution. CPU per Gbps is the number that survives hardware
changes.

## 6.4 Control-plane and scale benchmarks

Throughput is the number in the epic but not the one that decides
deployability. These are missing from the backlog entirely.

| # | Measurement | Rough target |
|---|---|---|
| C1 | `router create` to forwarding traffic, p50/p95/p99. Octavia's equivalent is ~5 min to ACTIVE plus up to 10 for the agent, and it is their most complained-about number | p95 under 2 min |
| C2 | `router add subnet` to the interface forwarding — the hotplug path ([§2.7](02-hooking-neutron.md#27-port-attachment)) | p95 under 30 s |
| C3 | Config change to applied, end to end. Vendordata alone was **measured** at 1 s | under the guest poll interval |
| C4 | Routers per manager at a fixed reconcile interval ([§5.5](05-lifecycle.md#55-scale-and-manager-availability)) | 1000+ |
| C5 | 50 concurrent router creates — do we melt Nova | No failures, graceful queueing |
| C6 | Vendordata endpoint with N idle instances — the unmeasured half of experiment 01 | No measurable impact on unrelated boots |
| C7 | Appliance density per compute: RAM, vCPU, how many before tenant workloads suffer | Report |
| C8 | Stage 2: tunnels per appliance, IKE rekey under churn. `pluto` scale is a different limit from ESP throughput | Report |

C1 and C7 decide adoption more than any Gbps figure. A router that takes ten
minutes to appear, or costs 2 GB of compute RAM each, is a technically correct
solution to a problem nobody will pay for.

## 6.5 Method

- **Zero-loss throughput by binary search** (RFC 2544 shape) with TRex or
  equivalent. iperf3 measures TCP goodput with its own stack in the loop and is
  a poor forwarding benchmark — smoke signal only.
- **IPsec needs a real peer.** A second appliance VM is more representative
  than an external endpoint and doubles as a stage 2 functional test.
- **Fix and record every knob**: virtio multiqueue, RSS/RPS/XPS, GRO/GSO/TSO,
  CPU pinning, hugepages, NUMA placement, `rp_filter`, MTU and MSS clamping,
  conntrack table size, kernel and libreswan versions. An unrecorded knob is an
  unreproducible result — see the experiment 06 and 07 methodology notes in
  [findings.md](findings.md), where exactly this produced two wrong
  conclusions.
- **Run B1 first.** If the OVN baseline on the lab hardware is below target,
  the target is about the hardware, not about us.

Tracked as OSPRH-40374.

## 6.6 When grout becomes relevant

Stage 4, strictly opt-in, triggered by a measured failure of the kernel to hit
the agreed target — not by preference. Constraints that affect whether it is
reachable from this design at all:

- In-guest DPDK needs the tenant vNICs bound away from the kernel (virtio PMD
  via `vfio-pci`, needing guest IOMMU, or `uio`). **The management NIC must
  stay kernel-bound**, which one-vNIC-per-interface accommodates naturally.
- **Hotplug and DPDK interact badly** — attaching a NIC to a running DPDK
  application is far harder than `ip link`. This may be where trunk ports stop
  being out of scope, since one trunked vNIC with VLAN subports makes the port
  set static.
- The agent's **backend abstraction must exist from stage 0** even with one
  implementation behind it. The PoC already had this shape (`LinuxBackend` plus
  an empty `GroutBackend`) and that part was right.
- ESP moves into the DPDK data plane, reducing libreswan to IKE with SAs
  installed into grout rather than XFRM. Significant work, scoped only once
  stages 0-2 are real.
