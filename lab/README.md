# Lab

A throwaway devstack VM pointed at this checkout, so that changing a line of
Python and seeing its effect takes seconds. **It is not RHOSO** and does not
pretend to be: it is ML2/OVN on one node, which is enough to exercise
everything stage pre-0 cares about.

## The inner loop

```sh
vim ../neutron_appliance/server/driver.py
./sync.sh        # rsync + pip install -e + restart neutron   (~10 s)
./smoke.sh       # the stage pre-0 acceptance scenario
./logs.sh        # follow the logs, filtered to our code
```

That is the whole point of this directory. Everything below is setup you do
once.

## First time

```sh
# one-off: an Ubuntu cloud image to clone from
mkdir -p ~/vm/images && curl -fsSL -o ~/vm/images/noble-base.img \
  https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img

./create-vm.sh                  # ~1 min, prints the VM address
vim env.sh                      # put that address in LAB_IP
./install-devstack.sh           # ~30-45 min, detached
./ssh.sh tail -f /opt/stack/logs/stack-run.log

./sync.sh                       # install this repo into the VM
./setup.sh                      # service project, flavors, provider
```

`create-vm.sh` generates `~/.ssh/id_devstack_appliance` if it is missing. A
dedicated passphrase-less key is deliberate: a smartcard key would demand a
physical touch on every scripted ssh.

Host requirements: nested virtualisation, libvirt `qemu:///system`, about
12 GB of spare RAM and 100 GB of disk. Override with
`VCPUS=4 RAM_MB=8192 ./create-vm.sh`.

## Scripts

| Script | What it does |
|---|---|
| `create-vm.sh` | Build the VM on local libvirt. `--recreate` to replace it |
| `install-devstack.sh` | Clone devstack, run `stack.sh` detached |
| `sync.sh` | **The inner loop.** rsync the checkout, `pip install -e` into devstack's venv, restart neutron, report which services failed to come back |
| `setup.sh` | Service project, `appliance-agent` role, nova flavor, image, neutron service provider, network flavor and profile. Idempotent |
| `smoke.sh` | The stage pre-0 acceptance scenario, as PASS/FAIL/SKIP checks |
| `logs.sh` | Follow neutron logs filtered to `appliance`, `Traceback` and `ERROR`. `-a` for everything |
| `cleanup.sh` | Delete what the lab creates, by ID |
| `ssh.sh` | Run a command in, or open a shell on, the VM |

## Things that will waste your afternoon

**`setup.sh` refuses to register the provider until the driver imports.**
This is deliberate. A `service_provider` line pointing at a class that does
not exist takes `neutron-api` down, and the reason appears in no obvious log.
Until milestone M2 exists, `setup.sh` configures everything else and says so.

**Never append `:default` to the `service_provider` line.** With the OVN L3
plugin, `_OvnPlusProviderConfiguration` swallows the resulting error at DEBUG
level and the built-in `ovn` provider silently disappears, so every
*unflavored* router breaks and nothing says why. `setup.sh` gets this right;
do not hand-edit it back in.

**`br-ex` loses its address when the VM reboots.** devstack does not restore
it, so `172.24.4.1` simply does not exist and every external data-path test
fails while the OVN configuration looks perfect. `smoke.sh` checks it and
tells you the fix:

```sh
sudo ip link set br-ex up
sudo ip addr replace 172.24.4.1/24 dev br-ex
```

**A leftover resource with the same name** makes every later
`openstack ... show <name>` fail with *"More than one ... exists"*, which is
indistinguishable from a timeout. Run `./cleanup.sh` between rounds.

**The VM does not autostart.** After a host reboot:

```sh
virsh -c qemu:///system start devstack-appliance
virsh -c qemu:///system domifaddr devstack-appliance   # the lease may have changed
```

devstack's systemd units come back on their own.

## What this lab deliberately does not do

It does not model RHOSO. The control plane here is a set of systemd units on
one host, not pods, so it cannot tell you anything about Multus, operators,
or whether the Neutron public endpoint is reachable from a tenant network in
a real deployment. Those questions are tracked in
[`../docs/open-questions.md`](../docs/open-questions.md).

The design research that produced this architecture, including eight scripted
experiments and their measurements, lives in the separate `neutron-stf-poc`
repository. [`../docs/findings.md`](../docs/findings.md) summarises what they
established; this lab is for building, not for measuring.
