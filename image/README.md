# Appliance image

The diskimage-builder recipe for the VM that backs a router. It installs the
agent, its systemd unit and the minimum runtime on a CentOS Stream 9 base.
There is no cloud-init and no per-instance data: the agent finds the config
drive itself, and every appliance boots from the same image.

## Build

```sh
make image
```

or, with the same defaults:

```sh
image/build.sh
```

Outputs, in `build/`:

| File | Contents |
|---|---|
| `neutron-appliance.qcow2` | the bootable image |
| `neutron-appliance.manifest.json` | provenance: base, agent version and commit, RPM and Python package versions, build command |

`build.sh` refuses a tree with tracked modifications, because the manifest
records `HEAD` and would otherwise describe code that is not in the image.

Prerequisites: `diskimage-builder` in `$PATH`, `qemu-img`, `git`, and a
working package network for the CentOS Stream repositories and PyPI
(**to verify**: the recipe has not yet been run end to end in this
repository).

## What the element does

- `centos-minimal` with `DIB_RELEASE=9-stream`; Python 3.9 is the system
  interpreter, matching the RHOSO baseline.
- `iproute`, `chrony`, and the Python runtime are RPMs. `chronyd` and
  `NetworkManager` are enabled with `systemctl` (**to verify**).
- The agent is installed from a `git archive` of `HEAD`, with its `agent`
  extra: `keystoneauth1`, `oslo.config`, `oslo.log`. The server dependency
  stack is absent by construction.
- `dhcp-all-interfaces` is used in NetworkManager-auto mode so the management
  NIC gets an address without cloud-init (**to verify**).
- The manifest is written to `/etc/neutron-appliance/image-manifest.json`
  inside the image and copied next to the `.qcow2`.

## Reproducibility

The manifest has no timestamp, host name or build path, and every list is
sorted. Two builds from the same commit, repository snapshot and package
index produce the same manifest; this is tested in
`tests/test_image_manifest.py`. The Python dependency versions are not
pinned: the manifest records what was installed, and a different PyPI state
is a different input.

## Manual boot check

The acceptance for M6 also requires the image to boot and the agent to log to
the console with no network. One way to check without Nova:

```sh
qemu-system-x86_64 -m 512 -nographic \
    -drive file=build/neutron-appliance.qcow2,if=virtio \
    -nic none
```

The console should show `neutron-appliance-agent: starting, agent 0.0.1,
boot ...` followed by a config-drive error and the backoff. That error is
expected without a config drive; what matters is that the agent runs and the
message reaches `/dev/console` (**to verify**).
