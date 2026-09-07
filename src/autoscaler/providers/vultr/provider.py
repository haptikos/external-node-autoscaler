"""Rent Cloud Compute and Cloud GPU instances from Vultr.

No SDK: the API is REST with a bearer token, so this talks to it with `requests`
directly rather than taking a dependency whose only job would be to set a header.
"""
import base64
import calendar
import os
import time

import requests

from ... import logging_setup
from ...resources import Resources
from ..base import DEFAULT_BATCH_SIZE, Machine, Provider, ProviderConfigError
from .. import cloudinit
from .config import VultrConfig, VultrPlacement

log = logging_setup.get("provider.vultr")

CREDENTIAL_VARS = ("VULTR_API_KEY",)

#: Vultr's word for "still provisioning, no address yet". It is NOT an empty
#: string, and a literal 0.0.0.0 reaching the kubeconfig is a peering that fails
#: on connect and reads like a network fault. See Machine.ip: None is the
#: correct way to say "not this pass".
UNASSIGNED_IP = "0.0.0.0"

#: An OVERRIDE for flavors whose GPU count the API does not give. Consulted
#: before the API, so an entry here also fixes one the API gets wrong.
#:
#: Usually empty. Vultr's `vcg` plans report `gpu_count` and gpu_count() reads
#: it; its `vdm` plans (dedicated metal, whole cards) report only `gpu_brand`,
#: and for those a flavor absent from this table gets NO gpu key at all, so the
#: loop rents one machine and measures the real allocatable. That costs one
#: machine, once, and cannot be wrong — which is why guessing from the flavor
#: name is not done here even though the name usually contains the answer.
#:
#: Add entries as you confirm them against a peered node's allocatable.
_GPU_PER_FLAVOR = {}

#: `gpu_brand` is on EVERY plan, carrying this string when there is no GPU. So
#: it is not a truthiness test: `if plan.get("gpu_brand")` is true for a 1-core
#: 512MB box.
NO_GPU = "none"

#: provision.sh installs the NVIDIA driver, container toolkit and device plugin.
#: An AMD plan needs ROCm and a different plugin, so it cannot advertise
#: nvidia.com/gpu whatever the count says.
NVIDIA = "nvidia"

#: A plan with no local disk. Renting one means creating a block volume first,
#: passing it at instance-create, and deleting it afterwards -- a second
#: resource with its own lifecycle, which leaks and keeps billing if a teardown
#: misses it. Not supported: such a flavor is excluded and named at startup.
#: `local_and_block_storage` is fine, it has a local disk to boot from.
BLOCK_ONLY = "block_storage"

_TIMEOUT = 30


class VultrApiError(RuntimeError):
    """A non-2xx from the API, carrying the status so callers can branch."""

    def __init__(self, status, message):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.message = message


def is_capacity_error(exc):
    """Vultr refusing to rent right now, rather than a real fault. See
    Provider.create: a shortage must not read like an outage.

    Unlike OVH this does not need prose matching in the ordinary case — a plan
    with no stock in a region is a 400 or a 503 with a plain message — but the
    status alone is not enough: 400 also covers a genuinely bad request, which
    must raise so a broken payload is not mistaken for a market condition.
    """
    if not isinstance(exc, VultrApiError):
        return False
    if exc.status == 503:
        return True
    if exc.status != 400:
        return False
    hints = ("not available", "out of stock", "no available", "sold out",
             "capacity", "quota", "limit")
    return any(h in exc.message.lower() for h in hints)


def gpu_brand(plan):
    """The GPU vendor, lowercased, or "" when the plan has none."""
    brand = (plan.get("gpu_brand") or NO_GPU).strip().lower()
    return "" if brand == NO_GPU else brand


def gpu_count(plan):
    """Devices Kubernetes will see, or None when the API does not say.

    Three shapes, and only one of them is an integer:

      * `vcg` plans carry `gpu_count` — but as a STRING, and a FRACTION for the
        shared ones: "1/4" of an A40 is a real value in this field.
      * `vdm` plans (dedicated metal, whole cards) carry no count at all.
      * everything else has no GPU.

    A fraction returns None rather than 1. The guest of a working vGPU does see
    one device, but provision.sh installs the stock apt driver, which cannot
    drive vGPU at all — so such a machine never finishes booting, and reporting
    a capacity for it would have the loop rent one every pass. flavor_capacity
    names it at startup instead.
    """
    raw = plan.get("gpu_count")
    if raw is None:
        return None
    text = str(raw).strip()
    if "/" in text:
        return None
    try:
        count = int(text)
    except ValueError:
        return None
    return count or None


def needs_block_storage(plan):
    """True when this plan cannot boot without a block volume supplied.

    Vultr answers a create for one with `400 Server add failed: One or more
    block devices are required.` -- a genuinely bad request, so is_capacity_error
    correctly refuses to read it as a stock shortage and it raises. Filtering
    here turns a per-pass traceback into one startup line.
    """
    return (plan.get("storage_type") or "").strip().lower() == BLOCK_ONLY


def public_ipv4(instance):
    """The address the hub will dial, or None while Vultr has not assigned one.

    `main_ip` is 0.0.0.0 for a pending instance, not absent, which is the trap:
    a truthiness check passes it straight through.
    """
    addr = (instance.get("main_ip") or "").strip()
    if not addr or addr == UNASSIGNED_IP:
        return None
    return addr


def created_epoch(instance):
    """RFC3339 UTC -> epoch seconds.

    calendar.timegm, not time.mktime: mktime reads the struct as LOCAL time, so
    on any pod not running in UTC every machine's age is wrong by the offset,
    which silently moves the boot-timeout deadline.
    """
    stamp = (instance.get("date_created") or "")[:19]
    if not stamp:
        # Unknown age reads as "just created", so the boot timeout gives it the
        # full window rather than reaping it on sight.
        return time.time()
    return calendar.timegm(time.strptime(stamp, "%Y-%m-%dT%H:%M:%S"))


class _Client:
    """The four calls this provider makes, and pagination."""

    def __init__(self, api_url, token):
        self.api_url = api_url
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {token}",
                                     "Content-Type": "application/json"})

    def _request(self, method, path, **kw):
        r = self.session.request(method, f"{self.api_url}{path}",
                                 timeout=_TIMEOUT, **kw)
        if r.status_code == 204 or not r.content:
            return {}
        try:
            body = r.json()
        except ValueError:
            body = {}
        if not r.ok:
            raise VultrApiError(r.status_code,
                                body.get("error") or r.text.strip()[:200])
        return body

    def get(self, path, **params):
        return self._request("GET", path, params=params or None)

    def post(self, path, payload):
        return self._request("POST", path, json=payload)

    def delete(self, path):
        return self._request("DELETE", path)

    def get_all(self, path, key, **params):
        """Every page of a list endpoint, followed through meta.links.next.

        Not optional: an account with more than one page of instances would
        otherwise hide the rest from every reap path, leaving machines running
        and billing with nothing tracking them.
        """
        out = []
        cursor = None
        while True:
            page = self.get(path, per_page=500,
                            **({"cursor": cursor} if cursor else {}), **params)
            out.extend(page.get(key) or [])
            cursor = ((page.get("meta") or {}).get("links") or {}).get("next")
            if not cursor:
                return out


class VultrProvider(Provider):
    def __init__(self, cfg, client=None):
        self.cfg = cfg
        self.client = client or self._client(cfg)
        self.batch_size = DEFAULT_BATCH_SIZE
        self.placements = {}
        self._os_id = cfg.os_id
        #: Plans are account-wide but their regional availability is not, so the
        #: catalog is cached per region.
        self._plans = {}

    @staticmethod
    def _client(cfg):
        """Build the client from the environment, and say so when it fails.

        The API key arrives as an env var from the Secret named by
        `credentialsSecret` — never from config. Missing, it would otherwise
        surface as a 401 on the first scale-up, which reads like a revoked key
        rather than an absent one.
        """
        token = os.environ.get("VULTR_API_KEY")
        if not token:
            raise ProviderConfigError(
                "missing environment: VULTR_API_KEY. It comes from the secret "
                "named by provider.credentialsSecret; check it exists and has "
                "that key.")
        return _Client(cfg.api_url, token)

    @classmethod
    def from_config(cls, cfg):
        return cls(VultrConfig.from_dict(cfg))

    @classmethod
    def parse_placement(cls, cfg):
        return VultrPlacement.from_dict(cfg)

    # ---------------------------------------------------------------- reads --
    def region_for(self, placement=None):
        return (placement.region if placement and placement.region
                else self.cfg.region)

    def regions(self):
        """The account region plus any a pool overrode to. Only used for the
        plan catalog — unlike OVH, /v2/instances is global, so a machine rented
        in an unexpected region is still visible to every reap path."""
        return sorted({self.cfg.region} | {
            p.region for p in self.placements.values() if p and p.region})

    def os_id(self):
        """Resolve `osName` to an os_id once, by substring match.

        Vultr's OS list is global, so unlike OVH's per-region image this is
        cached for the process rather than per region.
        """
        if self._os_id is None:
            wanted = self.cfg.os_name.lower()
            matches = [o for o in self.client.get_all("/os", "os")
                       if wanted in (o.get("name") or "").lower()]
            if not matches:
                raise RuntimeError(
                    f"no OS matching {self.cfg.os_name!r}; set osId to pin one")
            if len(matches) > 1:
                # Ambiguity is worth a line: picking the first is a coin flip
                # between, say, an image with and without cloud-init preloaded.
                log.warning("osName %r matches %d images (%s); using %s. Set "
                            "osId to pin one.", self.cfg.os_name, len(matches),
                            ", ".join(o["name"] for o in matches[:4]),
                            matches[0]["name"])
            self._os_id = int(matches[0]["id"])
        return self._os_id

    def plans(self, region):
        """The plan catalog for a region, keyed by plan id.

        `type=all` matters: the default response omits whole families, and a GPU
        plan missing from the catalog looks exactly like one that is out of
        stock.
        """
        if region not in self._plans:
            catalog = {}
            for p in self.client.get_all("/plans", "plans", type="all"):
                if region in (p.get("locations") or []):
                    catalog[p["id"]] = p
            self._plans[region] = catalog
        return self._plans[region]

    def flavor_capacity(self, placement):
        region = self.region_for(placement)
        catalog = self.plans(region)
        out = {}
        for name in placement.flavors:
            p = catalog.get(name)
            if p is None:
                log.warning("flavor %r not offered in %s; the pool cannot be "
                            "sized on it", name, region)
                continue
            if needs_block_storage(p):
                log.warning("flavor %r has no local disk (storage_type=%s) and "
                            "boots only from a block volume, which this "
                            "provider does not create. Vultr refuses it with "
                            "'One or more block devices are required'. It is "
                            "excluded and will never be rented -- pick a "
                            "local_storage flavor.", name, p.get("storage_type"))
                continue
            spec = {
                # Millicores and bytes, because Resources reads a bare int as
                # ALREADY canonical.
                "cpu": int(p["vcpu_count"]) * 1000,
                # MB. Vultr reports ram=2048 for a 2 GB plan, where OVH reports
                # ram=4 for a 4 GB one — the two are three orders of magnitude
                # apart and neither is wrong. Reading this as GB makes every
                # shape enormous, so packing puts the whole workload on one
                # machine and the pool never scales up.
                "memory": int(p["ram"]) * 1000 ** 2,
            }
            brand = gpu_brand(p)
            gpus = _GPU_PER_FLAVOR.get(name)
            if gpus is None and brand:
                gpus = gpu_count(p)

            if not brand:
                if placement.gpu:
                    log.warning("flavor %r is in a GPU pool but Vultr reports "
                                "no GPU on it", name)
            elif brand != NVIDIA:
                # Not merely unsized: provision.sh installs the NVIDIA stack, so
                # this machine boots without a working driver at all.
                log.warning("flavor %r is a %s GPU. provision.sh installs the "
                            "NVIDIA stack, so it will not advertise any GPU; "
                            "use an NVIDIA flavor or teach the script ROCm",
                            name, p.get("gpu_brand"))
            elif gpus:
                spec["nvidia.com/gpu"] = gpus
            elif "/" in str(p.get("gpu_count") or ""):
                log.warning("flavor %r is a FRACTIONAL vGPU (gpu_count=%s). The "
                            "guest driver for vGPU is licensed and not in "
                            "Ubuntu's apt repo, so provision.sh cannot drive "
                            "it and the machine will never finish booting. Use "
                            "a whole-card flavor.", name, p.get("gpu_count"))
            else:
                log.info("flavor %s is a GPU flavor (%s) whose device count "
                         "Vultr does not report; it will be measured when the "
                         "first machine peers", name, p.get("gpu_brand"))
            out[name] = Resources(spec)
        return out

    def list_machines(self, name_prefix=""):
        """Every instance this operator created.

        Vultr cannot filter by label prefix, so this filters client-side. The
        core filters again regardless.
        """
        out = {}
        for i in self.client.get_all("/instances", "instances"):
            label = i.get("label") or ""
            if not label.startswith(name_prefix):
                continue
            out[label] = Machine(
                name=label,
                id=i["id"],
                # Two fields, because they disagree in the case that matters: an
                # instance can be status=active with server_status=none while it
                # is still installing. Joined rather than picked so a log line
                # shows both.
                status=f"{i.get('status', '?')}/{i.get('server_status', '?')}",
                created=created_epoch(i),
                ip=public_ipv4(i),
            )
        return out

    # --------------------------------------------------------------- writes --
    def create(self, bootstrap, placement, flavors=None):
        """Rent the first plan with stock, booting it into the provision script.

        Returns the PLAN ID on success (truthy) so the caller can record what it
        actually got, False for no capacity. `flavors` restricts and orders the
        candidates -- the core passes only those big enough for the pods the
        machine is being rented for, so stock fallback can never silently hand
        back a machine too small to run them.
        """
        region = self.region_for(placement)
        user_data = cloudinit.build(bootstrap, self.cfg.provision_script,
                                    gpu=placement.gpu, provider=self.name)
        catalog = self.plans(region)
        for flavor in (flavors or placement.flavors):
            entry = catalog.get(flavor)
            # Both cases are named at startup by flavor_capacity, so skipping
            # quietly here is the difference between one log line and one per
            # pass forever.
            if entry is None or needs_block_storage(entry):
                continue
            try:
                self.client.post("/instances", {
                    "region": region,
                    "plan": flavor,
                    "os_id": self.os_id(),
                    # The label is the machine's identity everywhere -- k3s node
                    # name, Liqo cluster id, tenant namespace suffix, virtual
                    # node name -- and Vultr stores it verbatim. The hostname
                    # gets it too so the box agrees with the inventory.
                    "label": bootstrap.name,
                    "hostname": bootstrap.name,
                    # Base64, unlike OVH which takes the document raw. Wrong,
                    # cloud-init sees garbage and the machine boots into nothing
                    # -- which looks like a broken provision script.
                    "user_data": base64.b64encode(
                        user_data.encode("utf-8")).decode("ascii"),
                    # +20% per instance and Vultr's deploy form offers it by
                    # default. Worthless here: a rented machine is a k3s cluster
                    # of one, reaped when idle, holding nothing worth restoring.
                    # The strings are the API's, not booleans -- False would be
                    # neither value and the account default would apply.
                    "backups": "disabled",
                    "enable_ipv6": False,
                    # SSH keys are already in the cloud-config
                    # (cloudinit.build), so Vultr's own sshkey_id mechanism
                    # would mean pre-uploading them for no gain.
                })
            except VultrApiError as e:
                # Always logged, never a traceback, and no table of which
                # message means what. Vultr's own text is the diagnosis --
                # "out of stock", "open a support request for access", "invalid
                # os_id" all say what to do -- and a classifier over those
                # strings is a thing to maintain that goes stale in silence.
                #
                # The one split kept is severity, because it is the difference
                # between a normal Tuesday and something needing a person.
                if is_capacity_error(e):
                    log.warning("scale-up refused by Vultr for %s in %s: %s",
                                flavor, region, e.message)
                else:
                    log.error("Vultr refused %s in %s: %s", flavor, region,
                              e.message)
                # Next flavor either way, unlike OVH: there the binding limit is
                # a project-wide quota, so a smaller flavor fails the same. Here
                # every refusal is per flavor, and the fallback is the point.
                continue
            log.info("provisioning %s as %s in %s (pool %s)",
                     bootstrap.name, flavor, region, bootstrap.pool or "-")
            return flavor
        # Not an exception: stock runs out routinely, and False is what lets the
        # pool fall through to its next provider.
        log.warning("scale-up wanted but NO flavor available in %s: %s",
                    region, ", ".join(flavors or placement.flavors))
        return False

    def destroy(self, machine):
        try:
            self.client.delete(f"/instances/{machine.id}")
        except VultrApiError as e:
            if e.status != 404:
                raise
            # Already gone by another path. The caller's job is to reach a
            # state, not to be the one that got there.
            log.info("%s: already absent from Vultr", machine.name)
            return
        log.info("terminated instance %s", machine.name)

    # -------------------------------------------------------------- startup --
    def validate(self):
        """Force the OS and plan lookups now, so a bad key or a plan that is not
        sold in the region fails here rather than on the pass capacity was
        needed."""
        self.os_id()
        for region in self.regions():
            self.plans(region)
        log.info("vultr ready: regions=%s os_id=%s batch=%d",
                 ",".join(self.regions()), self._os_id, self.batch_size)
        for pool, placement in sorted(self.placements.items()):
            region = self.region_for(placement)
            catalog = self.plans(region)
            missing = [f for f in placement.flavors
                       if f not in catalog or needs_block_storage(catalog[f])]
            log.info("  pool %s: flavors=%s gpu=%s region=%s",
                     pool, ",".join(placement.flavors), placement.gpu, region)
            if missing:
                # Not fatal: a pool listing four plans where three are sold in
                # the region still scales. Silent, it looks like a stock
                # shortage that never clears.
                log.warning("  pool %s: flavor(s) %s cannot be rented in %s "
                            "(not sold there, or block-storage only)",
                            pool, ",".join(missing), region)
