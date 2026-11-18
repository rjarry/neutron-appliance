# neutron-appliance

Neutron L3 routers backed by **service VMs**, plugged into Neutron through the
Service Type Framework as an L3 service provider selected by router flavor.

A router created with the appliance flavor gets a dedicated Nova VM. Neutron
remains the IP address manager and the owner of desired state; an agent inside
the VM applies that state to a Linux data plane. Those packets never enter the
OVN pipeline.

**Plain routing is not the goal.** The goal is a place to terminate **IPsec
with libreswan**, which ML2/OVN has nowhere to run today. The router appliance
is the vehicle; IPsec is the cargo.

Tracked in Jira under
[OSPRH-40195](https://redhat.atlassian.net/browse/OSPRH-40195), child of
[RHOSSTRAT-1487](https://redhat.atlassian.net/browse/RHOSSTRAT-1487)
("STF: VPN As a Service").

## Status

**Stage pre-0: scaffolding.** Nothing forwards packets yet. The current
milestone is an end-to-end loop with no data plane — a flavored router boots a
VM, the agent syncs an empty configuration, and the operator can see it
converge. See [docs/plans/stage-pre-0.md](docs/plans/stage-pre-0.md).

## Documentation

| Document | Contents |
|---|---|
| [docs/intro.md](docs/intro.md) | **Start here.** Goal, terminology, how claims are tagged |
| [docs/01-goal.md](docs/01-goal.md) | What STF is, why a service VM, alternatives rejected, staging |
| [docs/02-hooking-neutron.md](docs/02-hooking-neutron.md) | Registration, events, sync versus async, port ownership |
| [docs/03-configuration.md](docs/03-configuration.md) | Desired state, delivery, management network, TLS, observability |
| [docs/protocol.md](docs/protocol.md) | The normative server/agent contract |
| [docs/04-first-draft.md](docs/04-first-draft.md) | Stage 0: static IPv4 routing |
| [docs/05-lifecycle.md](docs/05-lifecycle.md) | Reconciler, failure handling, HA, scale |
| [docs/06-performance.md](docs/06-performance.md) | Targets, benchmark matrices, method |
| [docs/open-questions.md](docs/open-questions.md) | Every unresolved decision |
| [docs/findings.md](docs/findings.md) | What the lab measured, and which assumptions were wrong |
| [docs/experiments.md](docs/experiments.md) | One section per experiment: question, method, result |
| [docs/prior-art.md](docs/prior-art.md) | Astara and Octavia, analysed |

The design research, the devstack lab and the eight experiments behind
`findings.md` live in a separate `neutron-stf-poc` repository.

## Layout

```
neutron_appliance/
├── common/     protocol and schema, shared. Must not import neutron
├── server/     driver, API extension, reconciler. Runs in neutron-server
└── agent/      runs inside the appliance VM
devtools/       check-spdx, check-patches, check-whitespace, commit-msg
docs/           design
└── plans/      implementation plans
etc/            systemd unit and sample configuration
lab/            throwaway devstack VM for iterating on the code
```

`common/` and `agent/` ship inside the appliance image and **must never import
Neutron**. This is enforced twice: `ruff` banned-api rules in
`neutron_appliance/*/ruff.toml`, which fail while you type, and
`tests/test_import_isolation.py`, which catches transitive imports the linter
cannot see.

## Development

```sh
make            # lint and tests
make format     # fix what ruff can fix
make lint       # spdx, whitespace, ruff, pylint, codespell
make tests      # pytest
make git-config # install the commit-msg hook
```

Everything runs in a `.venv` created on demand; there is nothing to install by
hand.

### Testing it for real

[`lab/`](lab/) builds a throwaway devstack VM pointed at this checkout, so
the edit-test loop is seconds rather than a RHOSO deployment:

```sh
make lab-sync    # rsync + pip install -e + restart neutron   (~10 s)
make lab-smoke   # the stage pre-0 acceptance scenario
make lab-logs    # follow the logs, filtered to our code
```

First-time setup, the traps worth knowing about, and what this lab
deliberately cannot tell you are in [lab/README.md](lab/README.md).

### Contributing

- Every source file carries `SPDX-License-Identifier: Apache-2.0` and a
  copyright line. `make lint` enforces both.
- Commit titles use a lowercase topic prefix (`agent: `, `docs: `), stay under
  72 characters and end without punctuation. `make check-patches` enforces
  this, and `make git-config` installs a hook that sorts your trailers.
- Every commit needs a `Signed-off-by` trailer and a body of at least three
  words explaining *why*.
- Tag factual claims in documentation as **measured**, **code-read**,
  **assumed** or **to verify**. Untagged text is design intent. When an
  experiment contradicts a document, change the document and record the
  correction.

## License

Apache-2.0. See [LICENSE](LICENSE).
