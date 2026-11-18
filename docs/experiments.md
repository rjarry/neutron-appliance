<sub>[Index](intro.md) · [Findings](findings.md) · [Open questions](open-questions.md)</sub>

# Experiments

One section per experiment: the question it answers, what it does, and what it
found. The scripts live in the **`neutron-stf-poc`** repository under
`lab/experiments/` and run inside its devstack VM; its `lab/README.md`
explains how to bring that up. The cross-cutting conclusions are in
[findings.md](findings.md).

| # | Script | Question | Result |
|---|---|---|---|
| 01 | `01-vendordata.sh` | Is `vendor_data2.json` re-fetched live, or cached? | Live, 1:1, no caching |
| 02 | `02-metadata-no-router.sh` | Does metadata reach a guest with no router? | Yes |
| 03 | `03-router-flavor-lsp.sh` | Is the phantom LSP inert or harmful? | Inert |
| 04 | `04-metainfo.sh` | Can `metainfo` carry per-flavor sizing? | Yes |
| 05 | `05-extgw-attach.sh` | Is the `project_id` workaround still needed? | Yes, and worse than thought |
| 06 | `06-mgmtnet-reachability.sh` | Does management option (b) work both ways? | Yes |
| 07 | `07-vendordata-blast-radius.sh` | What does a slow endpoint cost everyone else? | Much less than feared |
| 08 | `08-unowned-port-attach.sh` | Can we attach ports we do not own? | Yes, hot-attach only |

Run them with `./ssh.sh`, then
`cd /opt/stack/lab/experiments && ./NN-name.sh`. Results land in
`/opt/stack/lab/results/<experiment>/`. **Run `cleanup.sh` between rounds** —
a half-cleaned lab produces duplicate-name failures that look exactly like
timeouts.

---

## 01 — Dynamic vendordata is a live, per-request channel

**Question.** Everything in [§3.3](03-configuration.md) rests on vendordata
being re-read at request time. Is it? And does `metadata_cache_expiration`
bound how fast configuration converges?

**Method.** A stand-in endpoint (`lab/probe/vendordata_probe.py`) registered as
a `vendordata_dynamic_targets` provider, reporting two counters: `generation`
per instance, and `serial` incremented on every response served. A guest polls
`vendor_data2.json` while the probe's configuration is changed underneath it.
`serial` is the cache detector — if it stays constant for N seconds, N is the
floor on convergence latency.

**Result.**

| Measurement | Value |
|---|---|
| Guest fetches | 40 |
| Distinct `serial` values seen | **40** |
| Calls Nova made to the probe | **40** |
| Repeated serials | **0** |
| Config change to guest observing it | **1 s** |

A perfect 1:1. `metadata_cache_expiration = 15` was in force and had no
effect.

**New constraint found.** Nova wraps each dynamic target's response under its
configured name alongside StaticJSON's output —
`{"static": {}, "router": {...}}` — so the agent must unwrap `["router"]`.

**Not measured.** `calls_unknown_instance` was 0 because the cloud had one VM,
so the cost of answering `{}` for a whole cloud is still open
([§6.4](06-performance.md) C6).

---

## 02 — Metadata reaches a guest with no router

**Question.** Prerequisite for the boot-independence claim: can an appliance
read its configuration before it has a gateway?

**Method.** An isolated network (`10.77.0.0/24`, DHCP on, no router at all), a
guest booted on it, both metadata documents fetched.

**Result.** Yes. The network automatically gets an OVN metadata port:

```
port  69aaadee-...  device_owner network:distributed  ip 10.77.0.2
LSP   69aaadee-...  type localport  addresses "fa:16:3e:71:95:7c 10.77.0.2"
```

and the guest served both `meta_data.json` and
`vendor_data2.json -> {"static": {}, "router": {}}`. Metadata is per-network
and entirely independent of routing. The empty `router` object is the probe
correctly answering `{}` for an instance it does not know, confirming the
unknown-instance path and the target namespacing a second time.

---

## 03 — The phantom Logical_Switch_Port

**Question.** ML2/OVN creates an LSP for a flavored router's interface port
regardless of flavor. Does it blackhole traffic, or is it harmless?

**Method.** Register the in-tree `UserDefined` (LSP kept) and
`UserDefinedNoLsp` providers, create a router with each, and dump the
resulting LSP against a normal OVN router as the baseline.

**Result.** Inert.

```
type          : ""            vs. normal OVN router:  type      : router
addresses     : [unknown]                             addresses : [router]
port_security : []                                    options   : {router-port=lrp-...}
up            : false                                 up        : true
external_ids  : {... "neutron:cidrs"="10.88.0.1/24" ...}
```

Because `network:*` ports are trusted, `_get_allowed_addresses_from_port()`
returns empty and `_get_port_options()` falls through to
`addresses=['unknown']`. With no `MAC IP` entry there is no ARP responder
flow, so the gateway IP is not hijacked. The gateway IP does appear in
`external_ids["neutron:cidrs"]`, but `external_ids` generate no flows.

It remains a lying row — permanently `up: false`, reconciled over by
`ovn_db_sync` — which is why the upstream framing is "remove a hack" rather
than "fix a traffic bug". `UserDefinedNoLsp` behaves as documented: no LSP
after interface add.

---

## 04 — `metainfo` for per-flavor sizing

**Question.** The router API cannot carry arbitrary options. Can the service
profile's `metainfo` field carry VM sizing instead?

**Method.** Create a flavor profile with a JSON `metainfo`, read it back, then
push a 4000-byte payload at it.

**Result.** Round-trips byte-exact, and 4000 bytes is accepted (the column is
`String(4096)`).

```
$ openstack network flavor profile create --driver <...> \
    --metainfo '{"nova_flavor":"router.large","image":"appliance-1.2","vcpus":4}'
$ openstack network flavor profile show <id> -f value -c meta_info
{"nova_flavor":"router.large","image":"appliance-1.2","vcpus":4}
```

**Gotcha.** The API field is `metainfo`, the CLI column is `meta_info`.
`-c metainfo` fails with "No recognized column names". Write with
`--metainfo`, read with `-c meta_info`.

`get_flavor_next_provider()` still returns only `{driver, provider}`, so a
driver has to read the service profile itself.

---

## 05 — The external gateway attach failure

**Question.** The original PoC patched `ports.project_id` with raw SQL to make
Nova accept a router gateway port. Is that still necessary?

**Method.** Create a real router with an external gateway, inspect the
resulting `network:router_gateway` port, boot an instance and try to attach it.

**Result.** Still broken. The L3 plugin creates the port with an empty
`project_id`, and Nova rejects the attach:

```
BadRequestException: 400 ... POST /compute/v2.1/servers/<id>/os-interface
Port 074a3636-6b2c-454b-981f-14fb1477408b not usable for instance fc4f4d70-...
```

> **Methodology note.** The first version of this script created an *ordinary*
> port on the external network. Such a port inherits the caller's
> `project_id`, so the attach succeeded and the experiment reported a
> confident **false PASS**. Reproducing the failure requires a real
> `network:router_gateway` port.

Superseded by experiment 08, which showed the problem is broader than this
and found a fix.

---

## 06 — Management network option (b)

**Question.** Can the control plane reach an appliance on a tenant network
whose gateway is a default-flavour OVN router, without Octavia's
`lb-mgmt-net` apparatus?

**Method.** Tenant network `192.168.254.0/24` behind an OVN-flavoured router
with an external gateway. Test all four directions.

**Result.** Works both ways.

| Direction | Result |
|---|---|
| VM to OpenStack API | `keystone_http=300` (version discovery) |
| VM to metadata | full `meta_data.json` |
| Host to VM via floating IP | works |
| Host to VM direct, via a host route | works |

The reverse direction needs exactly a route
(`ip route add 192.168.254.0/24 via <router-gw-ip>`) and a security group rule.
**SNAT does not need to be disabled**, unlike octavia-operator's link router,
because SNAT governs tenant egress rather than control-plane-initiated
ingress.

> **Methodology note, and a genuine near-miss.** The first three
> reverse-direction tests — host route, SNAT disabled, floating IP — *all*
> reported failure, and the obvious reading was "option (b) cannot do
> controller to guest, exactly the Octavia problem". That reading was wrong.
> The default security group was dropping ingress ICMP, confounding every
> variant identically. Three consistent failures looked like a solid negative
> result and were one uncontrolled variable.

---

## 07 — Vendordata blast radius

**Question.** Our endpoint is called for every `vendor_data2.json` request in
the cloud. What does it cost unrelated workloads when it is slow or dead?

**Method.** Time both `meta_data.json` and `vendor_data2.json` from a guest in
three phases: probe healthy, probe stopped, probe delayed by 2000 ms. Check the
probe's own call counter in each phase.

**Result.**

| Phase / endpoint | n | ok | median ms | max ms |
|---|---|---|---|---|
| healthy / `meta_data` | 10 | 10 | 6 | 8 |
| healthy / `vendor_data2` | 10 | 10 | 48 | 50 |
| probe down / `meta_data` | 10 | 10 | 4 | 56 |
| probe down / `vendor_data2` | 10 | 10 | 6 | 7 |
| probe slow 2 s / `meta_data` | 10 | 10 | **6** | 60 |
| probe slow 2 s / `vendor_data2` | 10 | 10 | **2048** | 2050 |

1. `meta_data.json` never touches our service — a 2 s delay changed it by
   0 ms, and 30 `meta_data.json` fetches produced 0 probe calls.
2. `vendor_data2.json` is exactly 1:1 — the delay appeared as +2000 ms.
3. A dead endpoint breaks nothing: 10/10 succeeded on both endpoints, with
   `vendor_data2.json` returning in 6 ms because Nova gives up immediately on
   connection refused.

> **Methodology note.** v1 of this experiment timed only `meta_data.json` and
> concluded "a degraded probe is absorbed; limited blast radius". The
> conclusion was accidentally right and the evidence was worthless — it had
> measured an endpoint that never calls the probe. The 2 s delay producing
> *exactly* 0 ms impact is what flagged it; a genuinely "absorbed" result
> would have shown some variance. Measuring both endpoints side by side is
> what made the result mean anything.

---

## 08 — Port ownership

**Question.** Nova refuses to attach a port whose `project_id` differs from
the instance's. How big is that problem really, and can we work around it
without raw SQL or an upstream change?

**Method.** A dedicated service project stands in for the appliance owner. A
tenant router is created with an external gateway and one internal subnet, so
both Neutron port types exist with their real owners. Then: attach each of
them to an appliance in the service project (expected to fail), and attach
*our own* port — created in the service project with `--no-fixed-ip
--disable-port-security` — instead. Finally the guest configures a
Neutron-allocated address on that NIC by MAC and pings the external gateway,
and a separate step tries the same port at boot rather than on attach.

**Result.** The problem is broader than experiment 05 suggested, and the fix
works.

| Step | Outcome |
|---|---|
| Attach `network:router_gateway` (`project_id` empty) | rejected |
| Attach `network:router_interface` (`project_id` = tenant's) | **rejected** — this is the new finding |
| Attach our ipless service-project port | **accepted** |
| ML2/OVN binding | `up: true`, `type: ""`, `addresses: [unknown]`, `port_security: []` |
| Guest configures `172.24.4.242/24` by MAC, pings gateway | **OK** |
| Host pings the guest at that address | **OK**, 0% loss |
| Same port supplied at `servers.create()` | **rejected**: `requires a FixedIP in order to be used` |

So an appliance can carry an address Neutron allocated to a port it does not
own and which is attached to nothing — but every data port must be
hot-attached, never supplied at boot. See [§2.9](02-hooking-neutron.md).

> **Two methodology traps, both of which produced a confident false negative
> on the first run.**
>
> 1. **Do not test with a default-flavour OVN router's gateway IP.** That
>    router's `lrp-5b6356e6... networks=172.24.4.57/24` owns the same address
>    and answers ARP for it, so the guest is shadowed and the ping fails for
>    reasons that have nothing to do with the port model. A real
>    appliance-flavoured router has no LRP. The script now reserves a separate
>    address and asserts that no LRP claims it.
> 2. **devstack's `br-ex` loses its address across a VM reboot.** With no
>    `172.24.4.1` on the host, every external ping fails while the OVN
>    configuration looks perfect. The script now checks this as a
>    precondition. This also silently invalidates any rerun of experiment 06
>    until `br-ex` is restored:
>    ```
>    sudo ip link set br-ex up
>    sudo ip addr replace 172.24.4.1/24 dev br-ex
>    ```
>
> A third, cheaper trap: a leftover server with the same name makes every
> later `openstack server show <name>` fail with "More than one Server
> exists", which is indistinguishable from a boot timeout. The script now
> deletes by ID and waits.
