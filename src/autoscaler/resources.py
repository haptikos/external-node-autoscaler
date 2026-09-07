"""Capacity and demand as vectors, not as one number.

Every quantity here is in ONE canonical unit per dimension: cpu in millicores,
memory in bytes, everything else whole units.
"""
from kubernetes.utils import parse_quantity

from . import logging_setup

log = logging_setup.get("resources")

CPU = "cpu"
MEMORY = "memory"
PODS = "pods"


def _canonical(name, value):
    """A Kubernetes quantity as an int in this dimension's canonical unit."""
    q = parse_quantity(value)
    return int(q * 1000) if name == CPU else int(q)


class Resources:
    """An immutable resource vector. A missing key means ZERO for demand and
    UNKNOWN for capacity — the caller decides which, because the difference
    decides whether a pod is unschedulable or merely unmeasured."""

    __slots__ = ("_d",)

    def __init__(self, mapping=None):
        # No **kwargs: resource names carry dots and slashes
        # ("nvidia.com/gpu"), so they were never expressible that way.
        d = {}
        for k, v in (mapping or {}).items():
            k = str(k)
            d[k] = v if isinstance(v, int) else _canonical(k, v)
        self._d = {k: v for k, v in d.items() if v}

    @classmethod
    def from_api(cls, mapping):
        """A resources block as the Kubernetes API returns it."""
        out = cls()
        out._d = {str(k): _canonical(str(k), v) for k, v in (mapping or {}).items()}
        out._d = {k: v for k, v in out._d.items() if v}
        return out

    def keys(self):
        return self._d.keys()

    def get(self, name, default=0):
        return self._d.get(name, default)

    def __getitem__(self, name):
        return self._d[name]

    def __contains__(self, name):
        return name in self._d

    def __bool__(self):
        return bool(self._d)

    def __eq__(self, other):
        return isinstance(other, Resources) and self._d == other._d

    def __repr__(self):
        if not self._d:
            return "Resources()"
        return "Resources(" + ", ".join(
            f"{k}={self.human(k)}" for k in sorted(self._d)) + ")"

    def human(self, name):
        v = self._d.get(name, 0)
        if name == CPU:
            return f"{v}m"
        if name == MEMORY:
            return f"{v // (1024 ** 2)}Mi"
        return str(v)

    # ------------------------------------------------------------- algebra --
    def _combine(self, other, sign):
        out = Resources()
        keys = set(self._d) | set(other._d)
        out._d = {k: self._d.get(k, 0) + sign * other._d.get(k, 0) for k in keys}
        out._d = {k: v for k, v in out._d.items() if v}
        return out

    def __add__(self, other):
        return self._combine(other, 1)

    def __sub__(self, other):
        return self._combine(other, -1)

    def clamp_zero(self):
        out = Resources()
        out._d = {k: v for k, v in self._d.items() if v > 0}
        return out

    def is_zero(self):
        return not any(v > 0 for v in self._d.values())

    def fits_in(self, capacity, known_only=True):
        """Does this demand fit inside `capacity`?

        `known_only` decides what an absent capacity dimension means. True (the
        default) treats it as UNMEASURED and lets the pod through, because a
        flavor whose GPU count we never learned is not a node with
        zero GPUs. False
        treats absent as zero, which is what an observed, complete node vector
        deserves.
        """
        for k, v in self._d.items():
            if v <= 0:
                continue
            if k not in capacity:
                if known_only:
                    continue
                return False
            if v > capacity[k]:
                return False
        return True

    def dominant_ratio(self, capacity):
        """The largest fraction of any single capacity dimension this consumes.
        Used to pack the awkward pods first."""
        worst = 0.0
        for k, v in self._d.items():
            c = capacity.get(k, 0)
            if c > 0:
                worst = max(worst, v / c)
            elif v > 0:
                worst = max(worst, 1.0)
        return worst


def _sum(vectors):
    total = Resources()
    for v in vectors:
        total = total + v
    return total


def _elementwise_max(vectors):
    out = Resources()
    for v in vectors:
        merged = dict(out._d)
        for k, val in v._d.items():
            merged[k] = max(merged.get(k, 0), val)
        out._d = merged
    return out


def container_requests(containers):
    return _sum(Resources.from_api(
        (c.resources.requests if c.resources else None) or {})
        for c in (containers or []))


def pod_requests(pod):
    """A pod's effective request, plus one `pods`.

    Kubernetes' rule, not a simplification of it: init containers run to
    completion one at a time before the app containers start, so a pod's floor
    is the LARGER of the app total and the biggest init container. Summing the
    app containers alone under-sizes a pod with a heavy init step, and that pod
    is exactly the one that will not fit on the node we rented for it.
    """
    spec = pod.spec
    app = container_requests(getattr(spec, "containers", None))
    init = _elementwise_max([Resources.from_api(
        (c.resources.requests if c.resources else None) or {})
        for c in (getattr(spec, "init_containers", None) or [])])
    effective = _elementwise_max([app, init])
    overhead = Resources.from_api(getattr(spec, "overhead", None) or {})
    return effective + overhead + Resources({PODS: 1})
