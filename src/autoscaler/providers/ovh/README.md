# ovh

Rents single GPU instances from OVH Public Cloud and boots them into the
provisioning script. This is the provider the pilot runs on.

Config keys and their defaults: `config.py`, split in two. `OvhConfig` is the
ACCOUNT (project, region, image) and comes from the `providers` entry;
`OvhPlacement` is what one POOL rents (flavors, gpu, an
optional region override) and comes from that pool's provider entry. An example
values block: `values.example.yaml`.

Credentials: `OVH_ENDPOINT`, `OVH_APPLICATION_KEY`, `OVH_APPLICATION_SECRET`,
`OVH_CONSUMER_KEY`, from the Secret named by that entry's `credentialsSecret`
and injected with `envFrom` — never from config.

## What OVH Public Cloud is

OpenStack underneath, with a REST API of OVH's own on top. Everything this
provider touches hangs off one project: `/cloud/project/<32-hex-id>/instance`,
`/flavor`, `/image`. Instances are ordinary VMs — root, own kernel, cloud-init,
public IPv4 — so `../provision.sh` runs here unchanged. Billing is hourly.

**Four credentials, not one.** An endpoint (`ovh-eu`, `ovh-ca`, `ovh-us`, …), an
application key, an application secret and a consumer key. The consumer key is
minted separately and has to be **validated once in a browser** before it works;
a fresh, never-validated key fails exactly like a wrong one. All four go in the
Secret — the `ovh` SDK reads them from the environment itself.

**Everything is region-scoped.** Flavors, images and instances are all listed
per region, and a flavor offered in GRA may not exist in SBG. `regions()`
therefore unions the account region with every region a pool overrode to: a list
that asked only the account region would not see a machine rented elsewhere, and
invisible means unreapable and still billing.

**Images resolve by substring match** on the name (`Ubuntu 24.04`), once per
region, cached for the process. `validate()` forces that lookup at startup so a
bad region or bad credentials fail there rather than on the first scale-up.

**GPU flavor names encode the size**: `<card>-<ram_gb>`, and the card count
scales with it — `a10-45` is one A10, `a10-90` two, `a10-180` four. That
convention is the only source for the count, because the flavor API does not
report it (below).

## Things that have actually bitten

- **Stock, not quota, is the usual scale-up failure.** A10s are frequently
  unavailable in a region. `create()` walks `flavors` in order and returns
  `False` when none has stock — deliberately not an exception. Watch for
  `ALL flavors out of stock` if capacity never arrives.
- **`available` is advisory.** A flavor can report available and still fail to
  launch; the instance lands in an error state and the boot timeout cleans it up.
- **Timestamps must be parsed as UTC.** `created` is RFC3339 and is converted
  with `calendar.timegm`, never `time.mktime` — `mktime` reads it as local time,
  so in a non-UTC pod every machine's age is off by the offset, which silently
  moves the boot-timeout deadline.
- **The flavor API reports vcpus and ram but NOT gpu count.** Hence the
  hand-kept `_GPU_PER_FLAVOR`. A flavor absent from it gets **no** gpu key
  rather than zero: zero makes every GPU pod in the pool permanently
  unschedulable, absent makes the loop rent one and measure it.
- **`ram` is GB here; Vultr's is MB.** Reading it as MB made every shape smaller
  than the pool's reserve, so it clamped to nothing and the pool rented a
  machine per pod. `_sanity_check_shapes` catches a repeat at startup.
- **`flavors` has no default.** It used to fall back to `a10-45`, so a placement
  that omitted the key rented a GPU box by accident. Missing or empty is now a
  startup error naming the pool.
- **Missing credentials look like a region typo.** The SDK raises
  `InvalidRegion` about an endpoint of `None`; `_client()` catches that and
  names the absent variables instead.

## Not this provider's job

Firewall rules and the k3s/Liqo install are in the cloud-init template
(`../provision.sh`, shared with every other raw-VM provider), which ships in the
image as a ConfigMap. Editing one is `make autoscaler`; editing this directory
is `make image`.

