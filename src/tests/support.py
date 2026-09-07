"""Stubs and builders shared by the tests.

Deliberately hand-written rather than Mock-based: these stand in for the two
things that used to make this code untestable — a Kubernetes API and a cloud —
and an assertion that a Mock "was called" would not have caught either of the
bugs these tests exist for. The stubs hold real state and the tests assert on
what happened to it.
"""
import logging
import sys
import types
from pathlib import Path

from kubernetes import client as k8s

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from autoscaler.config import (Config, HubConfig, LiqoConfig,  # noqa: E402
                               ProviderSpec, ScaleConfig)
from autoscaler.pools import Pool, PoolProvider  # noqa: E402
from autoscaler.resources import Resources  # noqa: E402

# Silence the rules under test. Raising the ROOT level, not logging.disable():
# assertLogs() overrides a named logger's level but cannot override
# logging.disable()'s module-wide threshold.
logging.getLogger().setLevel(logging.CRITICAL)


#: No labels, no taints, so every pod matches it and the arithmetic under test
#: is the only variable. No reserves, for the same reason.
DEFAULT_POOL = Pool(name="default", max_machines=3,
                    system_reserve=Resources(), daemonset_reserve=Resources(),
                    providers=(PoolProvider(name="fake", priority=10),))


def make_pool(name="default", **over):
    """A pool with one fake provider and NO reserves.

    Zero reserve on purpose: it makes a test's arithmetic exactly the numbers
    written in the test. The real defaults are non-zero and are covered in
    test_config, where they are the subject rather than the background.
    """
    kw = dict(name=name, max_machines=3,
              system_reserve=Resources(), daemonset_reserve=Resources(),
              providers=(PoolProvider(name="fake", priority=10),))
    kw.update(over)
    return Pool(**kw)


def make_config(**over):
    """A Config with sane test defaults. Timers are short so tests need no sleeps."""
    groups = {
        "hub": dict(namespace="external-node-autoscaler",
                    condemned_configmap="condemned"),
        "liqo": dict(version="v1.2.0", liqoctl_bin="/bin/true", namespace="liqo",
                     peer_timeout="10m", consumer_id="hub",
                     max_pods_per_machine=50),
        "scale": dict(max_machines=3, idle_s=600, dead_s=900,
                      boot_s=1200, missing_confirm_s=90),
    }
    for group, cls in (("hub", HubConfig), ("liqo", LiqoConfig),
                       ("scale", ScaleConfig)):
        groups[group].update(over.pop(group, {}))
        groups[group] = cls(**groups[group])

    # The prefix is what MATCHES; the provider segment is appended when a name
    # is generated. Machines below are named external-ovh-000N to keep the
    # fixtures readable — they match on `external-` like any real machine would.
    base = dict(log_level="CRITICAL", interval=30, name_prefix="external-",
                watch_namespaces=("inference",),
                pools=(DEFAULT_POOL,),
                providers=(ProviderSpec(name="fake", batch_size=3),),
                hub_egress_ips=(), ssh_public_keys=(), probe_timeout_s=5)
    base.update(groups)
    base.update(over)
    base["pools"] = tuple(base["pools"])
    base["providers"] = tuple(base["providers"])
    return Config(**base)


class StubHub:
    """The hub as the controller sees it: numbers in, effects recorded."""

    def __init__(self, demand=0, vnodes=None, used=None, credentials=None,
                 condemned=(), pool="default", unroutable=(),
                 stuck_pods=()):
        #: An int is demand for `pool`; a dict is {pool name -> units}.
        self.demand = demand
        #: Where a bare int demand goes, and the pool stamped on seeded
        #: credentials that carry none.
        self.pool = pool
        self.unroutable = list(unroutable)
        #: What stuck_peering_pods() reports. Empty by default so every test
        #: that is not about this sees a healthy hub; None makes the read RAISE.
        self.stuck_pods = stuck_pods
        self.vnodes = vnodes or {}
        self.used = used or {}
        #: machine -> Credentials. Written by provision() now, not pushed by the
        #: machine, so tests seed it to model a fleet that already exists.
        self.credentials = dict(credentials or {})
        self._condemned = set(condemned)
        self.dropped = []
        self.unmarked = []
        self.endpoints = {}
        self.flavors = {}
        #: Stands in for the API server's clock. Credentials are aged by the
        #: Secret's own creationTimestamp in production — the one clock that
        #: survives an operator restart — so the stub has to stamp them too or
        #: the orphan rule is untested.
        self.clock = lambda: 1_000_000.0

    def pending_demand(self):
        """An int means that many one-pod-sized requests, which is what most
        tests mean by "demand"; a dict of lists states the vectors outright."""
        if isinstance(self.demand, dict) and any(
                isinstance(v, list) for v in self.demand.values()):
            return dict(self.demand), list(self.unroutable)
        counts = (dict(self.demand) if isinstance(self.demand, dict)
                  else {self.pool: self.demand})
        return ({k: [(f"{k}-{i}", Resources({"pods": 1, "cpu": "1"}))
                     for i in range(n)]
                 for k, n in counts.items()},
                list(self.unroutable))

    def stuck_peering_pods(self, min_age_s):
        if self.stuck_pods is None:
            raise RuntimeError("api down")
        return [p for p in self.stuck_pods if p["age_s"] >= min_age_s]

    def virtual_nodes(self):
        out = {}
        for n, v in self.vnodes.items():
            node = dict(v)
            node.setdefault("pool", self.pool)
            if not isinstance(node.get("capacity"), Resources):
                # Fixtures say "capacity: 2" meaning two one-CPU pods' worth.
                node["capacity"] = Resources(
                    {"pods": 50, "cpu": f"{node.get('capacity', 0)}"})
            out[n] = node
        return out

    def used_on(self, node, pool=None):
        u = self.used.get(node, 0)
        return u if isinstance(u, Resources) else Resources(
            {"pods": u, "cpu": f"{u}"} if u else {})

    def machine_credentials(self):
        out = {}
        for name, creds in self.credentials.items():
            if creds.pool is None:
                creds.pool = self.pool
            out[name] = creds
        return out

    def write_credentials(self, machine, creds, pool=None):
        creds.created = self.clock()
        creds.pool = pool
        self.credentials[machine] = creds

    def set_endpoint(self, machine, endpoint):
        self.endpoints[machine] = endpoint

    def set_flavor(self, machine, flavor):
        creds = self.credentials.get(machine)
        if creds is not None:
            creds.flavor = flavor
        self.flavors[machine] = flavor

    def secret_name(self, machine):
        return f"k3s-{machine}"

    def drop_secret(self, machine):
        self.dropped.append(machine)
        self.credentials.pop(machine, None)

    def condemned(self):
        return set(self._condemned)

    def unmark_condemned(self, machine):
        self.unmarked.append(machine)
        self._condemned.discard(machine)


class StubLiqo:
    def __init__(self, peered=(), sliced=(), ready=None, vnode_delay=0):
        self._peered = set(peered)
        self._sliced = set(sliced)
        #: Machines whose boot script has finished. None means "all of them",
        #: which is what most tests want; pass a set to model a machine that is
        #: still installing packages.
        self._ready = ready
        #: How many times a machine must be asked before its VirtualNode is
        #: reported settled. 0 settles on the first ask, which is what most
        #: tests want; 1 models the real thing, where Liqo creates the
        #: VirtualNode asynchronously seconds after the ResourceSlice.
        self.vnode_delay = vnode_delay
        self._label_asks = {}
        self.peered_calls = []
        self.sliced_calls = []
        self.labelled = []
        #: peer/label calls in the order they happened. `labelled` answers "was
        #: it labelled"; this answers "before or after the peering that could
        #: have stalled for the whole liqoctl timeout".
        self.events = []
        self.ready_calls = []
        self.unpeered = []
        self.torn_down = []
        #: force_teardown calls made as a routine post-unpeer sweep rather than
        #: as a rescue. Kept apart so a test can tell the two intents apart.
        self.swept = []
        self.unpeer_raises = False
        # What production actually does: liqoctl returns success while the
        # ForeignCluster is still present, its finalizers clearing
        # asynchronously against a remote we are about to destroy.
        self.unpeer_leaves_residue = False

    def machine_ready(self, machine, kubeconfig):
        self.ready_calls.append(machine)
        return True if self._ready is None else machine in self._ready

    def peerings(self):
        return set(self._peered)

    def slices(self):
        return set(self._sliced)

    def peer(self, machine, kubeconfig):
        self.peered_calls.append(machine)
        self.events.append(("peer", machine))
        self._peered.add(machine)

    def ensure_slice(self, machine, kubeconfig, pool=None, units=1):
        self.sliced_calls.append(machine)
        self._sliced.add(machine)

    def ensure_node_metadata(self, machine, pool):
        self.labelled.append((machine, pool.name))
        self.events.append(("label", machine))
        self._label_asks[machine] = self._label_asks.get(machine, 0) + 1
        return self._label_asks[machine] > self.vnode_delay

    def unpeer(self, machine, kubeconfig):
        if self.unpeer_raises:
            raise RuntimeError("remote unreachable")
        self.unpeered.append(machine)
        if not self.unpeer_leaves_residue:
            self._peered.discard(machine)

    def force_teardown(self, machine, expected=False):
        self.torn_down.append(machine)
        if expected:
            self.swept.append(machine)
        self._peered.discard(machine)
        self._sliced.discard(machine)


class FakeClock:
    """Time the tests move by hand, so a 600s idle timer costs no wall-clock."""

    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds
        return self.now


def pod(phase="Running", ns="inference", gpus=1, terminating=False,
        unschedulable=False, daemonset=False, requests=None, name="p",
        node_name=None, created=None, why=None):
    """Just enough of a V1Pod for the accounting code under test."""
    conditions = []
    if unschedulable:
        conditions.append(types.SimpleNamespace(
            type="PodScheduled", status="False", reason="Unschedulable",
            message=why))
    if requests is None:
        requests = {"nvidia.com/gpu": gpus} if gpus else {}
    owners = ([types.SimpleNamespace(kind="DaemonSet", name="ds")]
              if daemonset else [])
    return types.SimpleNamespace(
        metadata=types.SimpleNamespace(
            name=name, namespace=ns, owner_references=owners,
            #: An epoch float dressed up with .timestamp(), which is the only
            #: thing the code calls on it. None models a pod the API has not
            #: stamped yet.
            creation_timestamp=(types.SimpleNamespace(timestamp=lambda c=created: c)
                                if created is not None else None),
            deletion_timestamp=object() if terminating else None),
        status=types.SimpleNamespace(phase=phase, conditions=conditions),
        spec=types.SimpleNamespace(
            containers=[types.SimpleNamespace(
                resources=types.SimpleNamespace(requests=requests))],
            init_containers=[], overhead=None, node_name=node_name,
            node_selector=None, tolerations=[], affinity=None),
    )


def tenant_ns(name, machine=None, labelled=True):
    """A Liqo tenant Namespace. `machine=None` takes the owner from the name;
    `labelled=False` drops the remote-cluster-id label."""
    labels = {"liqo.io/tenant-namespace": "true"}
    if labelled:
        labels["liqo.io/remote-cluster-id"] = (
            machine if machine is not None else name[len("liqo-tenant-"):])
    return types.SimpleNamespace(
        metadata=types.SimpleNamespace(name=name, labels=labels))


def plain_ns(name, labels=None):
    """A namespace Liqo did NOT create. `labels` lets a test build the one that
    matters: a namespace NAMED like a tenant but not marked as one."""
    return types.SimpleNamespace(
        metadata=types.SimpleNamespace(name=name, labels=dict(labels or {})))


class StubCore:
    """The slice of CoreV1Api that hub.py's accounting actually calls.

    Namespaces are explicit rather than inferred from the pods, because
    stuck_peering_pods() now walks namespaces first: a test that forgets one is
    modelling a pod in a namespace that does not exist, and should see nothing.
    """

    def __init__(self, pods=(), namespaces=(), pod_errors=None):
        self._pods = list(pods)
        self._namespaces = list(namespaces)
        #: {namespace -> HTTP status} for reads that must FAIL. 404 is a
        #: namespace deleted between the two calls; anything else has to
        #: propagate rather than read as "nothing stuck here".
        self._pod_errors = dict(pod_errors or {})

    def list_namespace(self, label_selector=None):
        want = dict(kv.split("=", 1) for kv in label_selector.split(",")) \
            if label_selector else {}
        return types.SimpleNamespace(items=[
            n for n in self._namespaces
            if all((n.metadata.labels or {}).get(k) == v
                   for k, v in want.items())])

    def list_pod_for_all_namespaces(self, field_selector=None):
        return types.SimpleNamespace(items=list(self._pods))

    def list_namespaced_pod(self, ns):
        if ns in self._pod_errors:
            raise k8s.exceptions.ApiException(status=self._pod_errors[ns])
        return types.SimpleNamespace(
            items=[p for p in self._pods if p.metadata.namespace == ns])
