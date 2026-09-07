"""An in-memory provider. Rents nothing, bills nothing.

Two jobs:

  * It is what the controller tests drive, so the eight reconcile rules can be
    exercised without AWS, OVH or a cluster.
  * It is the proof that the core has no provider coupling left. Point a pool at
    `fake`, remove every OVH credential from the environment, and the operator
    must still start and reconcile. If it cannot, something core is still
    reaching for OVH.

Machines "boot" instantly and never become virtual nodes on their own, so
against a real hub this will scale up and then reap on the boot timeout. That
is the correct behaviour for a provider that cannot actually produce a machine.
"""
import itertools
import time
from dataclasses import dataclass

from ... import logging_setup
from ...resources import Resources
from ..base import Machine, Provider

log = logging_setup.get("provider.fake")


@dataclass(frozen=True)
class FakePlacement:
    """`capacity` is what the core reads, keyed by flavor in priority order."""
    capacity: dict = None
    out_of_stock: bool = False


class _Placements(dict):
    """Keyed by pool, falling back to one default so a test can build a
    provider by hand without knowing about pools."""

    def __init__(self, default):
        super().__init__()
        self.default = default

    def __missing__(self, key):
        return self.default

    def get(self, key, fallback=None):
        return self[key]


class FakeProvider(Provider):
    # Normally set by the loader from the directory name. Defaulted here because
    # the tests construct this directly, and the name becomes a segment of every
    # machine name — an "unnamed" segment in a test fixture is just confusing.
    name = "fake"

    def __init__(self, capacity=None, clock=time.time, out_of_stock=False,
                 assign_ip=True, create_raises=False, batch_size=3,
                 hide_created=False, list_raises=False):
        self.batch_size = batch_size
        #: What flavor_capacity reports when a test does not say otherwise.
        # One pod per machine by default, so a test asking for N pending pods
        # expects exactly N machines and the arithmetic needs no working out.
        self.default_capacity = capacity or {
            "fake-small": Resources({"cpu": "1", "pods": 1})}
        #: pool name -> FakePlacement, with a default for hand-built tests.
        self.placements = _Placements(FakePlacement())
        self.machines = {}
        self.out_of_stock = out_of_stock
        # A provider whose API is unreachable: its machines vanish from the
        # inventory without having been destroyed.
        self.list_raises = list_raises
        # False models the window where an instance is listed with no address.
        self.assign_ip = assign_ip
        # For the crash-window test: credentials must be persisted by the time
        # create() is reached.
        self.create_raises = create_raises
        # Created but withheld from list_machines: the gap between a successful
        # create() and the instance appearing in the inventory, where deleting
        # credentials is unrecoverable.
        self.hidden = set()
        self.hide_created = hide_created
        self.created = []        # names, in order — what the tests assert on
        self.created_flavors = []
        self.destroyed = []
        self._clock = clock
        self._ids = itertools.count(1)

    @classmethod
    def from_config(cls, cfg):
        return cls(out_of_stock=bool(cfg.get("outOfStock", False)))

    @classmethod
    def parse_placement(cls, cfg):
        return FakePlacement(
            capacity={k: Resources(v)
                      for k, v in (cfg.get("capacity") or {}).items()},
            out_of_stock=bool(cfg.get("outOfStock", False)))

    def flavor_capacity(self, placement):
        return dict(placement.capacity or self.default_capacity)

    def list_machines(self, name_prefix=""):
        if self.list_raises:
            raise RuntimeError("provider inventory unavailable (simulated)")
        return {n: m for n, m in self.machines.items()
                if n.startswith(name_prefix) and n not in self.hidden}

    def create(self, bootstrap, placement=None, flavors=None):
        if self.create_raises:
            raise RuntimeError("provider API blew up (simulated)")
        if self.out_of_stock or (placement and placement.out_of_stock):
            log.warning("out of stock (simulated)")
            return False
        n = next(self._ids)
        self.machines[bootstrap.name] = Machine(
            name=bootstrap.name, id=f"fake-{n}",
            status="ACTIVE", created=self._clock(),
            ip=f"198.51.100.{n}" if self.assign_ip else None)
        if self.hide_created:
            self.hidden.add(bootstrap.name)
        self.created.append(bootstrap.name)
        flavor = (flavors or list(self.flavor_capacity(placement)) or ["fake"])[0]
        self.created_flavors.append(flavor)
        log.info("created %s as %s", bootstrap.name, flavor)
        return flavor

    def destroy(self, machine):
        self.machines.pop(machine.name, None)
        self.destroyed.append(machine.name)
        log.info("destroyed %s", machine.name)

    def validate(self):
        log.info("fake provider ready — no machines will be rented and nothing "
                 "will be billed")
