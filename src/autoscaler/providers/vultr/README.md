# vultr

Rents Cloud Compute and Cloud GPU instances from Vultr and boots them into the
shared provisioning script. Both are ordinary VMs — root, own kernel, cloud-init,
public IPv4 — so `providers/provision.sh` runs here byte-for-byte as it does on
OVH.

Config keys and their defaults: `config.py`, split in two. `VultrConfig` is the
ACCOUNT (region, OS, API url) and comes from the `providers` entry;
`VultrPlacement` is what one POOL rents (flavors, gpu, an optional region
override) and comes from that pool's provider entry. An example values block:
`values.example.yaml`.

Credentials: `VULTR_API_KEY`, from the Secret named by that entry's
`credentialsSecret` and injected with `envFrom` — never from config.

## What Vultr is

A flat REST API (`/v2`) with a single bearer token — one credential, against
OVH's four. Cloud Compute and Cloud GPU are the **same** resource,
`/v2/instances`, which is why one code path serves both; Bare Metal is a
different one and is not supported (below). Billing is hourly with a monthly
cap. The OS is chosen by name and resolved to an `os_id` at startup.

**Machine types are "plans"**, catalogued at `/v2/plans` and filtered per region
by each plan's `locations` list. A plan absent from the pool's region is not out
of stock, it simply does not exist there.

**The plan prefix does not tell you what you are renting.** `vc2` is general
purpose, `vhf`/`vhp` high frequency and performance, `voc` optimised, `vcg`
Cloud GPU, `vdm` dedicated whole-card — but `vcg-a40-24c-120g-48vram` is
`type=vdm`, and half the `vx1` family has no local disk while the other half
does. Read `type`, `gpu_brand` and `storage_type` off the live response; every
mistake below came from trusting the name instead.

**`flavors`, not `plans`.** Vultr's own word for a machine type is "plan", and
this provider used to take it. It no longer does: the key an operator fills in
means the same thing in every provider's block, so a machine list can move
between them and a pool holding two providers reads one way. `plans` is rejected
at startup rather than accepted as an alias, and `tests/test_provider_vocabulary.py`
holds every provider to it. The translation to Vultr's vocabulary happens in
`provider.py`, at the API call.

No SDK. The API is REST with a bearer token, so this uses `requests` directly
rather than depending on a package whose only job would be to set a header.

## Bare Metal is not supported, deliberately

A different API (`/v2/bare-metals`, `/v2/plans-metal`), so a second code path
for no gain: provisioning eats a large part of `bootSeconds`, and its billing
minimums fight an idle reaper that releases after `idleSeconds`.

## Things to watch

- **`ram` is MB; OVH's is GB.** `ram: 2048` here is a 2 GB plan. Reading it as
  GB makes every shape enormous and the pool never scales up.
- **`main_ip` is `0.0.0.0` while provisioning, not empty.** A truthiness check
  passes it into the kubeconfig and the peering fails looking like a network
  fault. `public_ipv4()` maps it to `None`.
- **`user_data` must be base64.** OVH takes the cloud-config raw. Get it wrong
  and cloud-init sees garbage, which looks like a broken provision script.
- **The GPU field is `gpu_brand`, and it is on all 150 plans** carrying the
  literal `"none"`. Compare against `NO_GPU`, never truthiness.
- **`gpu_count` is a string and may be a fraction** (`"1/4"` of an A40) on
  `type=vcg`, and is absent entirely on `type=vdm` whole-card plans. Either way
  the flavor gets no gpu dimension and the loop measures a real machine instead.
- **Fractional vGPU plans can never finish booting.** Full-GPU is passthrough
  and `provision.sh` works; fractional is NVIDIA vGPU, whose *guest* driver is
  licensed and not in apt, so the machine joins advertising zero GPUs. Telling
  them apart is not guesswork: a `vcg` plan whose `gpu_count` contains `/` is
  fractional, every `vdm` is a whole card. Use whole-card plans — a fractional
  one gets no gpu dimension and is named in a startup warning.
- **AMD plans (`mi325x`, `mi355x`) are refused a gpu dimension outright.**
  `provision.sh` installs the NVIDIA stack, so such a machine has no driver at
  all rather than an unknown count.
- **A plan with no local disk cannot be rented.** Half the `vx1` family is
  `storage_type: block_storage` and Vultr refuses a plain create. Check
  `storage_type`, not the prefix; `needs_block_storage()` filters them.
- **`_GPU_PER_FLAVOR` is an override consulted before the API**, for a plan it
  cannot size or gets wrong. Normally empty.
- **A plan absent from the pool's region is invisible, not out of stock.**
  `validate()` names those at startup rather than leaving a shortage that never
  clears.
- **Stock is per flavor per region, and falling through is the point.**
  `create()` tries the next flavor, then the pool's next provider. Watch for
  `NO flavor available` if capacity never arrives.
- **`create()` never raises on an API refusal** — it logs Vultr's own message
  and moves on. A shortage is WARNING (a normal Tuesday); anything else is ERROR
  (needs a person). There is deliberately no table mapping messages to meanings.
- **That False-not-raise split also stops credentials leaking.** The machine's
  CAs are written *before* the machine is asked for; a clean False drops them
  now, an exception means the outcome is unknown so they are held for rule 6.
- **The most common ERROR is an account gate**: `Please open a support request
  for access to this product.` Vultr gates high-end GPU and bare metal per
  account and nothing in the API predicts it, so there is no startup check to
  add — the line repeats every pass until you open the request.

## Not this provider's job

Firewall rules and the k3s/Liqo install are in `../provision.sh`, shared with
every other raw-VM provider. Editing it is `make autoscaler`; editing this
directory is `make image`.
