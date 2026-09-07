"""Every knob the operator has, in one place, validated at startup.

Two sources, split on secrecy:

  * Non-secret settings arrive as environment variables from the chart, and the
    fleet — which providers exist and which pools rent from them — arrives as a
    YAML file the chart mounts.
  * Credentials arrive as environment variables from a Kubernetes Secret and are
    never read here — the OVH client picks up OVH_APPLICATION_KEY and friends on
    its own. Nothing credential-shaped should ever be added to this module,
    because everything in it is safe to log.

Validation happens here rather than at the point of use.
"""
import logging
import os
import sys
from dataclasses import dataclass, field, replace

import yaml

from .pools import DEFAULT_MAX_MACHINES, Pool, PoolProvider, Taint
from .providers.base import DEFAULT_BATCH_SIZE
from .resources import Resources
from .pools import validate as validate_pools

DEFAULT_FLEET_CONFIG = "/config/fleet.yaml"

_POOL_KEYS = {"name", "nodeLabels", "taints", "minMachines", "maxMachines",
              "providers", "systemReserve", "daemonsetReserve"}
_PROVIDER_KEYS = {"name", "credentialsSecret", "batchSize", "config"}
_TAINT_KEYS = {"key", "value", "effect"}

#: The only keys this module reads out of a pool's provider entry. Everything
#: else is the placement, passed to the provider verbatim and validated by its
#: parse_placement — flavors are provider vocabulary, and the moment core config
#: knows them, adding a node source stops being a values change.
_POOL_PROVIDER_RESERVED = {"name", "priority"}


def env(name, default=None, required=False):
    v = os.environ.get(name, default)
    if required and not v:
        sys.exit(f"missing required env: {name}")
    return v


def _csv(raw):
    return tuple(x.strip() for x in str(raw).split(",") if x.strip())


def _valid_ipv4(addr):
    host, _, prefix = addr.partition("/")
    octets = host.split(".")
    return (len(octets) == 4
            and all(o.isdigit() and 0 <= int(o) <= 255 for o in octets)
            and (not prefix or (prefix.isdigit() and 0 <= int(prefix) <= 32)))


@dataclass(frozen=True)
class HubConfig:
    """How the operator talks to its own cluster.

    No endpoint, CA or bootstrap token: machines never call home. See pki.py.
    """
    namespace: str            # own namespace: Secrets, condemned CM
    condemned_configmap: str


@dataclass(frozen=True)
class LiqoConfig:
    version: str
    liqoctl_bin: str
    namespace: str
    peer_timeout: str
    consumer_id: str
    max_pods_per_machine: int

    #: How long a peering pod may sit Pending WITH NO VERDICT before it is
    #: logged. Reported only. Patience for the ambiguous case -- image pulling,
    #: webhook answering -- where elapsed time is the only evidence there is; a
    #: pod the scheduler has REFUSED is acted on at once and never waits this
    #: long. Under peer_timeout so it is said while the handshake still runs.
    stuck_pod_s: int = 300

    #: Kill a handshake once the scheduler has refused its gateway pod rather
    #: than waiting out peer_timeout. liqoctl exits non-zero either way and
    #: tears nothing down, so only the waiting is given up. false keeps the
    #: reporting and sits out the full timeout.
    abort_blocked_peering: bool = True

    #: Ceiling on how long a refused machine is left alone; it is retried sooner
    #: if the scheduler stops refusing its gateway pod. Without any hold the
    #: identical doomed handshake is remade every pass, in front of every
    #: machine behind it. Shorter than scale.boot_s so a machine the hub never
    #: makes room for is reaped rather than retried forever.
    blocked_retry_s: int = 600


@dataclass(frozen=True)
class ProviderSpec:
    """`config` is opaque — the provider parses it. `batch_size` is here
    because the core enforces it: a per-pass budget shared by every pool renting
    from this provider."""
    name: str
    config: dict = field(repr=False, default_factory=dict)
    batch_size: int = DEFAULT_BATCH_SIZE


@dataclass(frozen=True)
class ScaleConfig:
    #: Fleet-wide, over every pool. 0 means UNLIMITED — see pools.UNLIMITED. A
    #: ceiling composes across pools; a floor does not, which is why minMachines
    #: is per-pool only.
    max_machines: int
    idle_s: int
    dead_s: int
    boot_s: int
    # How long a machine must stay absent from the provider's inventory before
    # an orphan teardown believes it. Absence is a single API answer, and acting
    # on one answer once destroyed a healthy machine that was serving traffic.
    # Deliberately shorter than dead_s: this is a confirmation, not a grace
    # period for a machine to come back.
    missing_confirm_s: int


@dataclass(frozen=True)
class Config:
    log_level: str
    interval: int

    # Identity. Not cosmetic: a machine's name becomes its k3s node name, its
    # Liqo cluster id, its tenant namespace suffix and its virtual node name.
    #
    # Names are built as <prefix><provider>-<random>, e.g. external-ovh-1a2b, so
    # the box says where it came from. But FILTERING uses the prefix alone —
    # never the per-provider form. If the operator only recognised machines from
    # the currently-selected provider, switching provider would make every
    # machine the previous one rented invisible: unreapable, and still billing.
    #
    # It is also what keeps anything NOT matching it -- a hand-managed cluster,
    # somebody else's machines in the same account -- out of reach of every
    # teardown path. Loosen it and those become reapable.
    name_prefix: str

    watch_namespaces: tuple

    hub: HubConfig
    liqo: LiqoConfig
    scale: ScaleConfig

    #: Ordered: pool order is the tiebreak when a pod fits more than one.
    pools: tuple
    providers: tuple

    # Rendered into each machine's firewall allowlist and cloud-init by whichever
    # provider knows how. Empty egress means "from anywhere", which is correct in
    # public-subnet mode: there the hub leaves from each node's own auto-assigned
    # address, so pinning would sever the peering the next time a node rolls.
    hub_egress_ips: tuple
    ssh_public_keys: tuple

    # Seconds to wait on a rented machine's API before calling it not-ready. A
    # booting machine DROPs, so an unanswered probe spends the whole budget;
    # keep it well under `interval`.
    probe_timeout_s: int = 5

    #: Rent nothing, release everything. A FLAG, not maxMachines=0, because 0
    #: means unlimited — expressing a drain as a ceiling would invert it into
    #: renting without bound, on the one path where that is unrecoverable.
    drain: bool = False

    def pool(self, name):
        """The named pool, or None when it has left the config — a state the
        loop handles rather than an error."""
        for p in self.pools:
            if p.name == name:
                return p
        return None

    @classmethod
    def load(cls):
        providers, pool_list = _load_fleet_file(
            env("FLEET_CONFIG_FILE", DEFAULT_FLEET_CONFIG),
        )

        max_machines = int(env("MAX_MACHINES", str(DEFAULT_MAX_MACHINES)))
        if max_machines < 0:
            sys.exit(f"MAX_MACHINES={max_machines} is negative; use 0 for no "
                     f"limit")

        drain = str(env("DRAIN", "false")).lower() in ("1", "true", "yes")
        if drain:
            log_pools = ", ".join(p.name for p in pool_list
                                  if p.min_machines > 0)
            pool_list = tuple(replace(p, min_machines=0) for p in pool_list)
            # Runs before logging_setup.setup(), where the last-resort handler
            # still emits WARNING and above.
            logging.getLogger("config").warning(
                "DRAIN=true: renting nothing and releasing everything%s",
                f" (overriding minMachines on: {log_pools})"
                if log_pools else "")

        egress = []
        for raw in env("HUB_EGRESS_IPS", "").replace(",", " ").split():
            if _valid_ipv4(raw):
                egress.append(raw)
            else:
                # Fatal, not skipped. Silently dropping one address of an
                # allowlist yields a machine the hub cannot reach, discovered
                # half an hour later as a peering that never completes.
                sys.exit(f"HUB_EGRESS_IPS: {raw!r} is not an IPv4 address or CIDR")

        dead_s = int(env("DEAD_SECONDS", "900"))
        if dead_s < 300:
            # The hub evicts pods off a NotReady node after 300s and that
            # eviction is what creates the replacement. Reaping sooner races the
            # kube-controller-manager for the corpse.
            sys.exit(f"DEAD_SECONDS={dead_s} is below the hub's 300s eviction")

        return cls(
            log_level=env("LOG_LEVEL", "INFO"),
            interval=int(env("INTERVAL_S", "30")),
            name_prefix=env("NAME_PREFIX", "external-"),
            watch_namespaces=_csv(env("WATCH_NAMESPACES", "inference")),
            hub=HubConfig(
                namespace=env("POD_NAMESPACE", required=True),   # downward API
                condemned_configmap=env("CONDEMNED_CONFIGMAP",
                                        "external-node-autoscaler-condemned"),
            ),
            liqo=LiqoConfig(
                version=env("LIQO_VERSION", "v1.2.0"),
                liqoctl_bin=env("LIQOCTL_BIN", "/usr/local/bin/liqoctl"),
                namespace=env("LIQO_NAMESPACE", "liqo"),
                peer_timeout=env("PEER_TIMEOUT", "10m"),
                consumer_id=env("CONSUMER_CLUSTER_ID", "hub"),
                max_pods_per_machine=int(env("MAX_PODS_PER_MACHINE", "50")),
                stuck_pod_s=int(env("STUCK_POD_SECONDS", "300")),
                abort_blocked_peering=env(
                    "ABORT_BLOCKED_PEERING", "true").lower() == "true",
                blocked_retry_s=int(env("BLOCKED_RETRY_SECONDS", "600")),
            ),
            scale=ScaleConfig(
                max_machines=max_machines,
                idle_s=int(env("IDLE_SECONDS", "600")),
                dead_s=dead_s,
                boot_s=int(env("BOOT_SECONDS", "900")),
                missing_confirm_s=int(env("MISSING_CONFIRM_SECONDS", "90")),
            ),
            pools=pool_list,
            providers=providers,
            hub_egress_ips=tuple(egress),
            ssh_public_keys=tuple(
                k.strip() for k in env("SSH_PUBLIC_KEYS", "").splitlines()
                if k.strip()),
            probe_timeout_s=int(env("PROBE_TIMEOUT_S", "5")),
            drain=drain,
        )


def _label_value(v):
    """Bools lowercased, not str()'d: YAML reads `gpu: true` as a bool and
    str(True) is "True", so a selector saying "true" would never match and the
    pool would simply never scale."""
    if isinstance(v, bool):
        return str(v).lower()
    return str(v)


def _reject_unknown(where, got, allowed):
    unknown = set(got) - allowed
    if unknown:
        sys.exit(f"{where}: unknown key(s) {sorted(unknown)}; "
                 f"expected some of {sorted(allowed)}")


def _load_fleet_file(path):
    """Read the mounted fleet: what can be rented, and into which pool.

    Provider settings stay opaque; everything structural (names, bounds, labels,
    taints) is checked here, because a mistake in it otherwise surfaces only as
    a pool that never scales.
    """
    if not os.path.exists(path):
        sys.exit(f"no fleet config at {path} — set FLEET_CONFIG_FILE, or mount "
                 f"one with `providers:` and `pools:`")
    with open(path, encoding="utf-8") as fh:
        doc = yaml.safe_load(fh) or {}
    if not isinstance(doc, dict):
        sys.exit(f"{path}: expected a mapping, got {type(doc).__name__}")

    providers = _parse_providers(path, doc.get("providers"))
    pool_list = _parse_pools(path, doc.get("pools"))
    try:
        validate_pools(pool_list, {p.name for p in providers})
    except ValueError as e:
        sys.exit(f"{path}: {e}")
    return providers, pool_list


def _parse_providers(path, raw):
    if not isinstance(raw, list) or not raw:
        sys.exit(f"{path}: `providers:` must be a non-empty list")
    out = []
    seen = set()
    for i, entry in enumerate(raw):
        where = f"{path}: providers[{i}]"
        if not isinstance(entry, dict):
            sys.exit(f"{where}: expected a mapping")
        _reject_unknown(where, entry, _PROVIDER_KEYS)
        name = entry.get("name")
        if not name:
            sys.exit(f"{where}: `name` is required (a directory under "
                     f"autoscaler/providers/)")
        if name in seen:
            # Credentials arrive as environment variables, so two entries of one
            # type would silently share an account.
            sys.exit(f"{where}: provider {name!r} is declared twice; "
                     f"credentials arrive as environment variables, so two "
                     f"instances of one provider type would share them")
        seen.add(name)

        cfg = entry.get("config") or {}
        if not isinstance(cfg, dict):
            sys.exit(f"{where}: `config:` must be a mapping, got "
                     f"{type(cfg).__name__}")
        batch = int(entry.get("batchSize", 3))
        if batch < 1:
            sys.exit(f"{where}: `batchSize` must be at least 1, got {batch} — "
                     f"a budget of zero can never rent anything")
        out.append(ProviderSpec(name=str(name), config=cfg, batch_size=batch))
    return tuple(out)


def _parse_pools(path, raw):
    if not isinstance(raw, list) or not raw:
        sys.exit(f"{path}: `pools:` must be a non-empty list")
    out = []
    for i, entry in enumerate(raw):
        where = f"{path}: pools[{i}]"
        if not isinstance(entry, dict):
            sys.exit(f"{where}: expected a mapping")
        _reject_unknown(where, entry, _POOL_KEYS)
        name = entry.get("name")
        if not name:
            sys.exit(f"{where}: `name` is required")
        where = f"{path}: pool {name!r}"

        labels = entry.get("nodeLabels") or {}
        if not isinstance(labels, dict):
            sys.exit(f"{where}: `nodeLabels` must be a mapping")

        max_machines = int(entry.get("maxMachines", DEFAULT_MAX_MACHINES))
        out.append(Pool(
            name=str(name),
            node_labels={str(k): _label_value(v) for k, v in labels.items()},
            taints=_parse_taints(where, entry.get("taints")),
            min_machines=int(entry.get("minMachines", 0)),
            max_machines=max_machines,
            providers=_parse_pool_providers(where, entry.get("providers")),
            # Absent means the default, not zero: a pool that says nothing
            # must still leave room for k3s and Liqo.
            **({"system_reserve": _parse_resources(
                where, "systemReserve", entry["systemReserve"])}
               if entry.get("systemReserve") is not None else {}),
            **({"daemonset_reserve": _parse_resources(
                where, "daemonsetReserve", entry["daemonsetReserve"])}
               if entry.get("daemonsetReserve") is not None else {}),
        ))
    return tuple(out)


def _parse_resources(where, key, raw):
    """A resource block from values, e.g. {cpu: 1, memory: 2Gi}."""
    if not raw:
        return Resources()
    if not isinstance(raw, dict):
        sys.exit(f"{where}: `{key}` must be a mapping of resource to quantity")
    try:
        return Resources(raw)
    except Exception as e:                                      # noqa: BLE001
        sys.exit(f"{where}: `{key}` is not a valid resource mapping: {e}")


def _parse_taints(where, raw):
    if not raw:
        return ()
    if not isinstance(raw, list):
        sys.exit(f"{where}: `taints` must be a list")
    out = []
    for taint in raw:
        if not isinstance(taint, dict):
            sys.exit(f"{where}: each taint must be a mapping")
        _reject_unknown(f"{where}: taint", taint, _TAINT_KEYS)
        if not taint.get("key"):
            sys.exit(f"{where}: a taint has no `key`")
        out.append(Taint(key=str(taint["key"]),
                         value=str(taint.get("value", "")),
                         effect=str(taint.get("effect", "NoSchedule"))))
    return tuple(out)


def _parse_pool_providers(where, raw):
    """Where this pool rents, sorted by priority once here — applied in the
    wrong order it rents from the expensive place first and nothing about the
    bill says why."""
    if not isinstance(raw, list) or not raw:
        sys.exit(f"{where}: `providers:` must be a non-empty list")
    out = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            sys.exit(f"{where}: providers[{i}] must be a mapping")
        name = entry.get("name")
        if not name:
            sys.exit(f"{where}: providers[{i}] has no `name`")
        out.append(PoolProvider(
            name=str(name),
            priority=int(entry.get("priority", 100)),
            config={k: v for k, v in entry.items()
                    if k not in _POOL_PROVIDER_RESERVED},
        ))
    # Stable, so equal priorities keep declaration order.
    return tuple(sorted(out, key=lambda e: e.priority))
