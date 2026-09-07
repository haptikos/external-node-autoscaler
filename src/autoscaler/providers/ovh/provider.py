"""Rent single GPU instances from OVH Public Cloud."""
import calendar
import time

import ovh

from ... import logging_setup
from ...resources import Resources
from ..base import DEFAULT_BATCH_SIZE, Machine, Provider, ProviderConfigError
from .. import cloudinit
from .config import OvhConfig, OvhPlacement

log = logging_setup.get("provider.ovh")

CREDENTIAL_VARS = ("OVH_ENDPOINT", "OVH_APPLICATION_KEY",
                   "OVH_APPLICATION_SECRET", "OVH_CONSUMER_KEY")

#: Mirrors is_routable() in provision.sh: the machine puts its choice in
#: the serving cert's SANs and the operator puts its own in the kubeconfig, so a
#: disagreement fails the TLS handshake minutes later, looking like a network
#: fault.
_PRIVATE_PREFIXES = ("10.", "127.", "169.254.", "192.168.")

#: Substrings that make an OVH APIError mean "not right now". Matching on prose
#: because the SDK maps 400/403/404/409/460 to named exceptions and quota is
#: none of them — it falls through to a bare APIError with no error code. A miss
#: costs a logged traceback and no scale-up; the pass itself survives, which is
#: what makes a heuristic affordable here.
_CAPACITY_HINTS = ("quota", "not enough resources", "no capacity")

#: GPUs per flavor. OVH's flavor API reports vcpus and ram but NOT gpu count,
#: so it lives here rather than in values: it changes rarely, and a provider
#: whose API does report it simply never consults a table like this.
#:
#: A flavor absent from this map gets NO gpu key at all. That is deliberate --
#: see Provider.flavor_capacity. Zero would make every GPU pod in that pool
#: permanently unschedulable; absent makes the loop rent one and measure it.
_GPU_PER_FLAVOR = {
    "a10-45": 1, "a10-90": 2, "a10-180": 4,
    "a100-180": 1, "a100-360": 2, "a100-720": 4,
    "h100-380": 1, "h100-760": 2, "h100-1520": 4,
    "l4-90": 1, "l4-180": 2, "l4-360": 4,
    "l40s-90": 1, "l40s-180": 2, "l40s-360": 4,
    "rtx5000-28": 1, "rtx5000-56": 2, "rtx5000-112": 4,
    "t1-45": 1, "t1-90": 2, "t1-180": 4,
    "t2-45": 1, "t2-90": 2, "t2-180": 4,
}


def _is_routable(addr):
    octets = addr.split(".")
    if len(octets) != 4 or not all(o.isdigit() for o in octets):
        return False        # not IPv4 dotted-quad; IPv6 lands here too
    if addr.startswith(_PRIVATE_PREFIXES):
        return False
    return not (octets[0] == "172" and 16 <= int(octets[1]) <= 31)


def is_capacity_error(exc):
    """OVH refusing to rent right now, rather than a real fault. See
    Provider.create: a shortage must not read like an outage."""
    return any(h in str(exc).lower() for h in _CAPACITY_HINTS)


def public_ipv4(instance):
    """The address the hub will dial, or None while OVH has not assigned one —
    normal while an instance is BUILDING.

    Filtering on both `type` and `version` matters: picking blindly can yield a
    vRack or IPv6 address, either of which times out rather than fails.
    """
    for entry in instance.get("ipAddresses") or []:
        if entry.get("version") != 4 or entry.get("type") != "public":
            continue
        addr = entry.get("ip") or ""
        if _is_routable(addr):
            return addr
    return None


class OvhProvider(Provider):
    def __init__(self, cfg, client=None):
        self.cfg = cfg
        self.client = client or self._client()
        self.batch_size = DEFAULT_BATCH_SIZE
        self.placements = {}
        #: Per region: the image name resolves to a different id in each.
        self._image_ids = {}

    @staticmethod
    def _client():
        """Build the OVH client from the environment, and say so when it fails.

        Credentials arrive as env vars from the Secret named by
        `provider.credentialsSecret` — never from config. When that secret is
        missing or short a key, the SDK raises InvalidRegion complaining about
        an endpoint of `None`, which reads like a region typo and is nothing of
        the sort. Since the chart passes provider settings through untouched and
        can no longer catch this, the operator has to name it plainly.
        """
        try:
            return ovh.Client()
        except Exception as e:                                  # noqa: BLE001
            import os
            missing = [v for v in CREDENTIAL_VARS if not os.environ.get(v)]
            hint = (f"missing environment: {', '.join(missing)}" if missing
                    else f"{type(e).__name__}: {e}")
            raise ProviderConfigError(
                f"could not build the OVH client — {hint}. These come from the "
                f"secret named by provider.credentialsSecret; check it exists "
                f"and has all four keys.") from e

    @classmethod
    def from_config(cls, cfg):
        return cls(OvhConfig.from_dict(cfg))

    @classmethod
    def parse_placement(cls, cfg):
        return OvhPlacement.from_dict(cfg)

    # ---------------------------------------------------------------- reads --
    def _base(self):
        return f"/cloud/project/{self.cfg.project}"

    def region_for(self, placement=None):
        return (placement.region if placement and placement.region
                else self.cfg.region)

    def image_id(self, region=None):
        region = region or self.cfg.region
        if region not in self._image_ids:
            for img in self.client.get(f"{self._base()}/image",
                                       region=region, osType="linux"):
                if self.cfg.image_name.lower() in img["name"].lower():
                    self._image_ids[region] = img["id"]
                    break
            else:
                raise RuntimeError(
                    f"image {self.cfg.image_name!r} not found in {region}")
        return self._image_ids[region]

    def flavor_capacity(self, placement):
        region = self.region_for(placement)
        catalog = {f["name"]: f for f in
                   self.client.get(f"{self._base()}/flavor", region=region)}
        out = {}
        for name in placement.flavors:
            f = catalog.get(name)
            if f is None:
                log.warning("flavor %r not offered in %s; the pool cannot be "
                            "sized on it", name, region)
                continue
            spec = {
                # Millicores and bytes, because Resources reads a bare int as
                # ALREADY canonical. Handing it vcpus=12 would mean 12
                # millicores, and the pool would look 1000x smaller than it is.
                "cpu": int(f["vcpus"]) * 1000,
                # GB, matching the flavor's marketing spec: d2-4 reports ram=4,
                # not 4000. Reading it as MB made every shape's memory smaller
                # than the reserve, so it clamped away and the pool rented a
                # machine per pod. sanity_check_shapes catches a repeat.
                "memory": int(f["ram"]) * 1000 ** 3,
            }
            gpus = _GPU_PER_FLAVOR.get(name)
            if gpus:
                spec["nvidia.com/gpu"] = gpus
            elif placement.gpu:
                log.warning("flavor %r is in a GPU pool but is not in "
                            "_GPU_PER_FLAVOR; its GPU count is unknown until a "
                            "machine peers", name)
            out[name] = Resources(spec)
        return out

    def regions(self):
        """The account region plus any a pool overrode to. Listing only the
        account region would leave a machine rented elsewhere invisible:
        unreapable, and still billing."""
        return sorted({self.cfg.region} | {
            p.region for p in self.placements.values() if p and p.region})

    def list_machines(self, name_prefix=""):
        out = {}
        for region in self.regions():
            out.update(self._list_in(region, name_prefix))
        return out

    def _list_in(self, region, name_prefix):
        out = {}
        for i in self.client.get(f"{self._base()}/instance", region=region):
            if not i["name"].startswith(name_prefix):
                continue
            out[i["name"]] = Machine(
                name=i["name"],
                id=i["id"],
                status=i["status"],
                # OVH returns RFC3339 in UTC. calendar.timegm, not time.mktime:
                # mktime interprets the struct as LOCAL time, so on any pod not
                # running in UTC every machine's age was wrong by the offset —
                # which shifts the boot-timeout deadline in whichever direction
                # the timezone happens to point.
                created=calendar.timegm(
                    time.strptime(i["created"][:19], "%Y-%m-%dT%H:%M:%S")),
                ip=public_ipv4(i),
            )
        return out

    # --------------------------------------------------------------- writes --
    def create(self, bootstrap, placement, flavors=None):
        """Rent the first flavor with stock, booting it into provision.sh.

        Returns the FLAVOR NAME on success (truthy) so the caller can record
        what it actually got, False for no capacity. `flavors` restricts and
        orders the candidates -- the core passes only those big enough for the
        pods the machine is being rented for, so stock fallback can never
        silently hand back a machine too small to run them.
        """
        region = self.region_for(placement)
        user_data = cloudinit.build(bootstrap, self.cfg.provision_script,
                                    gpu=placement.gpu, provider=self.name)
        # `catalog`, not `flavors`: the parameter is called flavors, and
        # shadowing it here silently ignored the subset the core asked for and
        # rented whatever the catalog listed first.
        catalog = {f["name"]: f for f in
                   self.client.get(f"{self._base()}/flavor", region=region)}
        for fname in (flavors or placement.flavors):
            f = catalog.get(fname)
            if not (f and f.get("available", True)):
                continue
            try:
                self.client.post(f"{self._base()}/instance",
                                 flavorId=f["id"],
                                 imageId=self.image_id(region),
                                 region=region, name=bootstrap.name,
                                 userData=user_data, monthlyBilling=False)
            except ovh.exceptions.APIError as e:
                if not is_capacity_error(e):
                    raise
                # Quota is per-project and only ever enforced here, at the POST
                # — the flavor's `available` flag says nothing about it.
                # Returning rather than trying the next flavor: the binding
                # quota is total RAM or vCPU, so a smaller one fails the same.
                log.warning("scale-up refused by OVH for %s: %s", fname, e)
                return False
            log.info("provisioning %s as %s in %s (pool %s)",
                     bootstrap.name, fname, region, bootstrap.pool or "-")
            return fname
        # Not an exception: stock runs out routinely, and False is what lets
        # the pool fall through to its next provider.
        log.warning("scale-up wanted but ALL flavors out of stock in %s: %s",
                    region, ", ".join(flavors or placement.flavors))
        return False

    def destroy(self, machine):
        try:
            self.client.delete(f"{self._base()}/instance/{machine.id}")
        except ovh.exceptions.ResourceNotFoundError:
            # Already gone by another path. The caller's job is to reach a
            # state, not to be the one that got there.
            log.info("%s: already absent from OVH", machine.name)
            return
        log.info("terminated instance %s", machine.name)

    # -------------------------------------------------------------- startup --
    def validate(self):
        """Resolve the image in every region now, so bad credentials or a bad
        region fail here rather than on the pass capacity was needed."""
        for region in self.regions():
            self.image_id(region)
        log.info("ovh ready: project=%s regions=%s batch=%d",
                 self.cfg.project, ",".join(self.regions()), self.batch_size)
        for pool, placement in sorted(self.placements.items()):
            log.info("  pool %s: flavors=%s gpu=%s region=%s",
                     pool, ",".join(placement.flavors), placement.gpu,
                     self.region_for(placement))
