# external-node-autoscaler

The operator that rents machines, peers them into the EKS hub with Liqo, and
hands them back when demand drops. Named for what it is rather than what it
rents: it scales on whatever pending pods request — cpu, memory, GPUs, pod
count — so nothing here is GPU-specific and a CPU-only pool needs no special
mode. See `docs/scaling.md`. (The Helm release is still called
`external-node-autoscaler` — renaming one means uninstalling it.)

```
autoscaler/
  __main__.py       wiring + the loop; the only module that constructs anything
  config.py         every knob, validated at startup
  logging_setup.py  one handler, one format
  hub.py            Kubernetes accounting — demand, supply, secrets, tokens
  liqo.py           peering, resource slices, teardown
  controller.py     the eight reconcile rules, and only those
  providers/        where machines come from — one directory each
tests/              stdlib unittest; no cluster, no cloud, no credentials
```

`hub` / `liqo` / `controller` split on **failure domain**, not on size: they
break for different reasons and get fixed by different people. Every outage this
project has had so far was in `liqo.py`, which is why it is a file you can hold
in your head rather than a section of a thousand-line one.

Nothing is constructed at import time. `__main__.py` builds the API clients, the
provider and the controller and passes them down — which is the whole reason the
reconcile rules are testable. Before the split, importing the operator built a
Kubernetes client and an OVH client at module scope, so no part of it ran
without both sets of credentials.

## Adding a node provider

Create a directory. That is the entire integration:

```
providers/<name>/
  __init__.py          one line: PROVIDER = YourProvider
  provider.py          the four methods — its presence is what makes this a provider
  config.py            frozen dataclass + from_dict, owning the defaults
  requirements.txt     its pip deps; the Dockerfile finds them by globbing
  values.example.yaml  the keys that go under a providers[] entry's config
  README.md            the gotchas
```

Then add `- name: <name>` to `providers` in values, and name it from a pool's
`providers` list. There is **no registry to update, no chart template to edit and
no Dockerfile line to add** — providers are found by listing directories, and
both a provider's `config` and a pool's provider entry are passed through the
chart verbatim.

A provider used by a pool also implements `parse_placement`, which validates
that pool's entry (flavors and the like) at startup and returns an object
exposing `units_per_machine`. Same fail-fast contract as `from_config`: a typo
is a crash-loop naming the key, not a scale-up that quietly does nothing on the
pass capacity was needed.
Discovery does not import, so only the selected provider is loaded and an
unselected one's heavy dependencies cannot stop the operator from starting.

Copy `providers/ovh/` for the shape. Read `providers/base.py` for the contract;
the two invariants that matter are that the **name you are given is the
machine's identity everywhere** (k3s node name, Liqo cluster id, tenant
namespace, virtual node) and that **`create()` returns False for "no capacity"
and raises for everything else** — a GPU stock shortage is a normal Tuesday and
must not read like an outage.

## How a machine's bootstrap is assembled

`providers/provision.sh` is what the machine runs, shared by every raw-VM
provider because nothing in it is provider-specific. It is a real shell script:
no placeholders, every input arriving through `/root/provision.env`, which it
sources. `bash -n` and shellcheck therefore read it directly, and a provider
that does not use cloud-init at all could execute it over SSH unchanged. One
needing its own ships `providers/<name>/provision.sh` and points
`provisionScript` at it.

`providers/cloudinit.py` wraps it into a `#cloud-config` document for raw-VM
providers — as a dict handed to PyYAML, never by string substitution. That is
deliberate, because YAML assembled by concatenation fails in ways no YAML check
catches: a `runcmd` entry containing `": "` parses as a mapping, cloud-init
rejects the whole runcmd module, and the machine boots, looks healthy, never
installs k3s, and bills you while it does it. A serialiser cannot emit that.

`experiments/hack/validate-provision.py` runs the shell checks and the one contract
that spans both trees: the readiness label the script stamps must be the one the
operator waits for. Everything structural is in `tests/test_cloudinit.py`.

## Working on it

```
make test     # from experiments/ — unit tests in a venv it manages itself
make lint     # helm lint + terraform + the cloud-init cross-check
```

`make test` needs no cluster and no credentials: the controller runs against
`providers/fake/` and a stubbed hub. The suite is mostly regressions for bugs
that cost real money — a machine reaped while its own pod was still pulling
images, and a healthy serving box torn down because one inventory call came back
short. If you change `controller.py` or `hub.py`, those tests are the reason you
will find out before OVH does.

Configuration is environment variables (from the chart) plus
`/provider/fleet.yaml` (providers and pools, mounted). Credentials are env vars
from a Secret and are never read by `config.py` — everything in that module is
safe to log.
