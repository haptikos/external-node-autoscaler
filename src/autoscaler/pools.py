"""Pools: which kind of machine a pending pod is asking for.

A pool is one machine type made addressable. Pods reach it with an ordinary
nodeSelector or nodeAffinity, so no workload chart has to know this operator
exists.

Matching lives here rather than in the scheduler because at the moment a pod is
Unschedulable there is no node to match it against — that is why a machine is
being asked for. The prediction and the scheduler's later decision must agree,
or the operator rents a machine the scheduler then refuses to use.
"""
import math
from collections import namedtuple
from dataclasses import dataclass, field

from . import logging_setup
from .resources import Resources

log = logging_setup.get("pools")

#: A machine ceiling of 0 means NO LIMIT, not "rent nothing" — the one place
#: that decides it, so the fleet ceiling and every pool's agree.
UNLIMITED = 0

#: Default ceiling, fleet-wide and per pool. Unlimited: the bill is bounded by
#: demand, which is what a pod actually asks for, rather than by a number that
#: has to be raised every time a workload grows.
DEFAULT_MAX_MACHINES = UNLIMITED


#: Held back on every machine by default. A pool that sets nothing still leaves
#: room for the k3s server, Liqo's control plane and the per-node daemonsets —
#: advertising the whole machine starves them and the node goes NotReady under
#: load, which reads as a dead machine rather than an over-committed one.
DEFAULT_SYSTEM_RESERVE = {"cpu": "1", "memory": "2Gi"}
DEFAULT_DAEMONSET_RESERVE = {"cpu": "200m", "memory": "256Mi"}


def headroom(limit, used):
    """How many more machines a limit allows. inf when unlimited, so `min()`
    over several limits picks the real one without special cases."""
    return math.inf if limit == UNLIMITED else limit - used

#: Liqo stamps this on every virtual node it projects. A pool's labels are added
#: to it, so a pod selecting only `liqo.io/type` still matches an untainted pool.
VNODE_LABELS = {"liqo.io/type": "virtual-node"}

Taint = namedtuple("Taint", "key value effect")

#: Also Liqo's, on every virtual node.
VNODE_TAINT = Taint("virtual-node.liqo.io/not-allowed", "true", "NoExecute")

_EFFECTS = ("NoSchedule", "PreferNoSchedule", "NoExecute")


@dataclass(frozen=True)
class PoolProvider:
    """One place a pool can rent from. `config` is opaque — the provider parses
    it via parse_placement."""
    name: str
    priority: int = 100
    config: dict = field(repr=False, default_factory=dict)


@dataclass(frozen=True)
class Pool:
    name: str
    node_labels: dict = field(default_factory=dict)
    taints: tuple = ()
    min_machines: int = 0
    max_machines: int = DEFAULT_MAX_MACHINES
    providers: tuple = ()

    system_reserve: Resources = field(
        default_factory=lambda: Resources(DEFAULT_SYSTEM_RESERVE))
    daemonset_reserve: Resources = field(
        default_factory=lambda: Resources(DEFAULT_DAEMONSET_RESERVE))

    def reserve(self):
        return self.system_reserve + self.daemonset_reserve

    def schedulable(self, capacity):
        """What of a machine this size the scheduler may actually use."""
        return (capacity - self.reserve()).clamp_zero()

    def labels(self):
        return {**VNODE_LABELS, **self.node_labels}

    def effective_taints(self):
        return (VNODE_TAINT,) + tuple(self.taints)


# ------------------------------------------------------------------ matching --
def _matches_expression(expr, labels):
    """Gt/Lt are unsupported rather than approximated: returning False reports
    the pod unroutable instead of guessing a pool for it."""
    key = getattr(expr, "key", None)
    op = getattr(expr, "operator", None)
    values = list(getattr(expr, "values", None) or ())
    present = key in labels

    if op == "Exists":
        return present
    if op == "DoesNotExist":
        return not present
    if op == "In":
        return present and labels[key] in values
    if op == "NotIn":
        return not present or labels[key] not in values
    log.warning("unsupported nodeAffinity operator %r on key %r; treating as "
                "no match", op, key)
    return False


def _matches_term(term, labels):
    exprs = list(getattr(term, "match_expressions", None) or ())
    # matchFields selects on metadata.name; there is no node yet to have one.
    if getattr(term, "match_fields", None):
        log.warning("nodeAffinity matchFields cannot be matched against a pool; "
                    "treating as no match")
        return False
    return all(_matches_expression(e, labels) for e in exprs)


def affinity_matches(pod, labels):
    """Required node affinity only. Treating a `preferred` term as a constraint
    would refuse to rent a machine the scheduler would in fact have used."""
    affinity = getattr(pod.spec, "affinity", None)
    node_affinity = getattr(affinity, "node_affinity", None) if affinity else None
    required = getattr(
        node_affinity, "required_during_scheduling_ignored_during_execution",
        None) if node_affinity else None
    if not required:
        return True
    terms = list(getattr(required, "node_selector_terms", None) or ())
    if not terms:
        return True
    # Terms are OR-ed, expressions within a term AND-ed.
    return any(_matches_term(t, labels) for t in terms)


def selector_matches(pod, labels):
    selector = getattr(pod.spec, "node_selector", None) or {}
    return all(labels.get(k) == v for k, v in selector.items())


def tolerates(toleration, taint):
    """Kubernetes semantics, including the two empty-field rules: an empty
    `effect` tolerates every effect, and an empty `key` with operator Exists
    tolerates every taint."""
    effect = getattr(toleration, "effect", None)
    if effect and effect != taint.effect:
        return False
    key = getattr(toleration, "key", None)
    op = getattr(toleration, "operator", None) or "Equal"
    if not key:
        return op == "Exists"
    if key != taint.key:
        return False
    if op == "Exists":
        return True
    return getattr(toleration, "value", None) == taint.value


def tolerates_all(pod, taints):
    tolerations = list(getattr(pod.spec, "tolerations", None) or ())
    return all(any(tolerates(t, taint) for t in tolerations)
               for taint in taints)


def matches(pod, pool):
    labels = pool.labels()
    return (selector_matches(pod, labels)
            and affinity_matches(pod, labels)
            and tolerates_all(pod, pool.effective_taints()))


def match(pod, pools):
    """The pool this pod is asking for, or None.

    Labels and taints decide; declaration order only settles a pod that fits
    more than one. None is handled by Hub.pending_demand, which rents nothing
    for it.
    """
    for pool in pools:
        if matches(pod, pool):
            return pool
    return None


# --------------------------------------------------------------- validation --
def validate(pools, provider_names):
    """Startup checks. Raises ValueError naming the pool."""
    if not pools:
        raise ValueError("no pools defined — nothing can ever be rented")

    seen = set()
    for pool in pools:
        if not pool.name:
            raise ValueError("a pool has no name")
        if pool.name in seen:
            raise ValueError(f"duplicate pool name {pool.name!r}")
        seen.add(pool.name)

        if pool.max_machines < 0 or pool.min_machines < 0:
            raise ValueError(f"pool {pool.name!r}: machine bounds cannot be "
                             f"negative")
        if (pool.max_machines != UNLIMITED
                and pool.min_machines > pool.max_machines):
            # minMachines is proactive, so this rents to a floor and reaps
            # straight back down to a lower ceiling.
            raise ValueError(
                f"pool {pool.name!r}: minMachines={pool.min_machines} exceeds "
                f"maxMachines={pool.max_machines}")

        for taint in pool.taints:
            if taint.effect not in _EFFECTS:
                raise ValueError(
                    f"pool {pool.name!r}: taint effect {taint.effect!r} is not "
                    f"one of {', '.join(_EFFECTS)}")

        if not pool.providers:
            raise ValueError(f"pool {pool.name!r}: no providers — it can never "
                             f"rent anything")
        for entry in pool.providers:
            if entry.name not in provider_names:
                raise ValueError(
                    f"pool {pool.name!r}: provider {entry.name!r} is not "
                    f"declared under `providers:` "
                    f"(have: {', '.join(sorted(provider_names)) or 'none'})")

    _warn_on_shadowing(pools)


def _warn_on_shadowing(pools):
    """A pool an earlier untainted one already covers can never be selected, and
    the symptom is only that it never scales."""
    for i, pool in enumerate(pools):
        for earlier in pools[:i]:
            if not earlier.taints and _covers(earlier.labels(), pool.labels()):
                log.warning(
                    "pool %r can never be selected: %r is declared earlier, "
                    "has no taints, and carries every label %r does. Add a "
                    "taint to %r or reorder.",
                    pool.name, earlier.name, pool.name, earlier.name)
                break


def _covers(earlier_labels, later_labels):
    return all(earlier_labels.get(k) == v for k, v in later_labels.items())
