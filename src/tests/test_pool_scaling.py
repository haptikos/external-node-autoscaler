"""Per-pool arithmetic in the reconcile loop.

The rules that dispose of machines stay fleet-wide and are covered in
test_controller.py. What is here is everything that had to become per-pool, plus
the failure the split exists to prevent.
"""
import unittest
from collections import Counter

from support import (FakeClock, StubHub, StubLiqo, make_config, make_pool)
from autoscaler.config import ProviderSpec

from autoscaler.controller import Controller, UNKNOWN_POOL
from autoscaler.providers import flavor_shapes as provider_shapes
from autoscaler.pools import PoolProvider
from autoscaler.resources import Resources
from autoscaler.providers.base import Machine
from autoscaler.providers.fake.provider import FakePlacement, FakeProvider

GPU = make_pool("gpu-a10", node_labels={"pool": "gpu-a10"}, max_machines=5)
CPU = make_pool("cpu-dryrun", node_labels={"pool": "cpu-dryrun"},
                max_machines=5)


def build(hub, pools=(GPU, CPU), providers=None, cfg=None, clock=None,
          names=None, **cfg_over):
    clock = clock or FakeClock()
    names = iter(names or [f"{i:04x}" for i in range(100)])
    hub.clock = clock
    providers = providers or {"fake": FakeProvider(clock=clock)}
    cfg = cfg or make_config(
        pools=list(pools),
        providers=[ProviderSpec(name=n, batch_size=99) for n in providers],
        scale={"max_machines": 99}, **cfg_over)
    shapes = provider_shapes(providers, cfg.pools)
    ctrl = Controller(hub, StubLiqo(), providers, cfg, clock=clock,
                      flavor_shapes=shapes,
                      namer=lambda: next(names))
    return ctrl, clock


def ready(capacity=1, pool=None, since=0.0):
    return {"ready": True, "since": since, "capacity": capacity, "pool": pool}


def created_pools(hub):
    """Which pool each machine was rented for, in order."""
    return [c.pool for c in hub.credentials.values()]


class TheFailureThisPrevents(unittest.TestCase):
    def test_free_capacity_in_one_pool_does_not_satisfy_demand_in_another(self):
        """A pod asking for an A10 is not served by a free slot on a d2-8.

        Summed fleet-wide the deficit reads zero, nothing is rented, and the pod
        pends forever behind `demand=1 free=1` -- both numbers true, the
        conclusion wrong. This is the whole reason demand is split before it is
        summed.
        """
        hub = StubHub(demand={"gpu-a10": 1, "cpu-dryrun": 0},
                      vnodes={"external-fake-c1": ready(pool="cpu-dryrun")},
                      used={"external-fake-c1": 0})
        ctrl, clock = build(hub)
        ctrl.providers["fake"].machines["external-fake-c1"] = Machine(
            name="external-fake-c1", id="1", status="ACTIVE", created=clock(),
            ip="198.51.100.1")
        hub.credentials["external-fake-c1"] = _creds("cpu-dryrun")

        ctrl.reconcile()
        self.assertEqual(created_pools(hub).count("gpu-a10"), 1,
                         "the A10 pod was starved by an idle CPU machine")


class PerPoolBounds(unittest.TestCase):
    def test_a_pool_ceiling_binds_on_its_own(self):
        hub = StubHub(demand={"gpu-a10": 99, "cpu-dryrun": 0})
        ctrl, _ = build(hub, pools=(make_pool("gpu-a10", max_machines=2),))
        ctrl.reconcile()
        self.assertEqual(len(ctrl.providers["fake"].created), 2)

    def test_the_fleet_ceiling_binds_across_pools(self):
        """Two pools of 5 each, but the fleet allows 3. Whichever pools rent,
        the total is 3 -- and the second pool must see what the first took."""
        hub = StubHub(demand={"gpu-a10": 99, "cpu-dryrun": 99})
        cfg = make_config(
            pools=[GPU, CPU],
            providers=[ProviderSpec(name="fake", batch_size=99)],
            scale={"max_machines": 3})
        ctrl, _ = build(hub, cfg=cfg)
        ctrl.reconcile()
        self.assertEqual(len(ctrl.providers["fake"].created), 3)

    def test_min_machines_rents_proactively_with_no_demand_at_all(self):
        """A behaviour change: minMachines used to be only a floor on reaping,
        so a warm pool never got warm."""
        hub = StubHub(demand={"gpu-a10": 0, "cpu-dryrun": 0})
        ctrl, _ = build(hub, pools=(make_pool("gpu-a10", min_machines=2,
                                              max_machines=5),))
        ctrl.reconcile()
        self.assertEqual(len(ctrl.providers["fake"].created), 2)
        self.assertEqual(created_pools(hub), ["gpu-a10", "gpu-a10"])

    def test_min_machines_does_not_keep_renting_once_satisfied(self):
        hub = StubHub(demand={"gpu-a10": 0})
        pool = make_pool("gpu-a10", min_machines=2, max_machines=5)
        ctrl, clock = build(hub, pools=(pool,))
        ctrl.reconcile()
        for _ in range(3):
            clock.advance(30)
            ctrl.reconcile()
        self.assertEqual(len(ctrl.providers["fake"].created), 2)

    def test_machine_shape_is_read_per_pool(self):
        """Two pools renting different flavors from one provider must not share
        one shape."""
        provider = FakeProvider()
        provider.placements = {
            "gpu-a10": FakePlacement(capacity={
                "big": Resources({"cpu": "1", "pods": 1})}),
            "cpu-dryrun": FakePlacement(capacity={
                "small": Resources({"cpu": "4", "pods": 4})})}
        hub = StubHub(demand={"gpu-a10": 2, "cpu-dryrun": 4})
        ctrl, _ = build(hub, providers={"fake": provider})
        ctrl.reconcile()
        # 2 pods at one-per-machine, 4 pods at four-per-machine.
        self.assertEqual(created_pools(hub).count("gpu-a10"), 2)
        self.assertEqual(created_pools(hub).count("cpu-dryrun"), 1)


class BatchBudget(unittest.TestCase):
    def test_one_provider_budget_is_shared_across_pools_in_a_pass(self):
        """The budget bounds a blocking create loop and the blast radius of a
        bad flavor. Per-pool budgets would let N pools spend N times it."""
        hub = StubHub(demand={"gpu-a10": 99, "cpu-dryrun": 99})
        cfg = make_config(
            pools=[GPU, CPU],
            providers=[ProviderSpec(name="fake", batch_size=3)],
            scale={"max_machines": 99})
        ctrl, _ = build(hub, cfg=cfg)
        ctrl.reconcile()
        self.assertEqual(len(ctrl.providers["fake"].created), 3)

    def test_the_budget_resets_each_pass(self):
        hub = StubHub(demand={"gpu-a10": 99, "cpu-dryrun": 0})
        cfg = make_config(
            pools=[GPU], providers=[ProviderSpec(name="fake", batch_size=2)],
            scale={"max_machines": 99})
        ctrl, clock = build(hub, cfg=cfg)
        ctrl.reconcile()
        clock.advance(30)
        ctrl.reconcile()
        self.assertEqual(len(ctrl.providers["fake"].created), 4)


class ProviderFallback(unittest.TestCase):
    def pools(self):
        return (make_pool("gpu-a10", max_machines=5, providers=(
            PoolProvider(name="first", priority=10),
            PoolProvider(name="second", priority=20))),)

    def test_no_capacity_falls_through_to_the_next_provider(self):
        first = FakeProvider(out_of_stock=True)
        second = FakeProvider()
        hub = StubHub(demand={"gpu-a10": 1})
        ctrl, _ = build(hub, pools=self.pools(),
                        providers={"first": first, "second": second})
        ctrl.reconcile()
        self.assertEqual(first.created, [])
        self.assertEqual(len(second.created), 1)

    def test_priority_decides_the_order_not_declaration(self):
        cheap = FakeProvider()
        pricey = FakeProvider()
        pool = make_pool("gpu-a10", max_machines=5, providers=(
            PoolProvider(name="pricey", priority=50),
            PoolProvider(name="cheap", priority=10)))
        # config.py sorts on load; this pool is built by hand, so sort here too.
        pool = pool.__class__(**{**pool.__dict__, "providers": tuple(
            sorted(pool.providers, key=lambda e: e.priority))})
        hub = StubHub(demand={"gpu-a10": 1})
        ctrl, _ = build(hub, pools=(pool,),
                        providers={"cheap": cheap, "pricey": pricey})
        ctrl.reconcile()
        self.assertEqual(len(cheap.created), 1)
        self.assertEqual(pricey.created, [])

    def test_an_exception_stops_the_pool_rather_than_asking_the_next(self):
        """After an exception we do not know whether a machine was created, so
        asking a second provider could rent two."""
        first = FakeProvider(create_raises=True)
        second = FakeProvider()
        hub = StubHub(demand={"gpu-a10": 1})
        ctrl, _ = build(hub, pools=self.pools(),
                        providers={"first": first, "second": second})
        ctrl.reconcile()
        self.assertEqual(second.created, [], "rented twice after an exception")

    def test_a_raising_provider_does_not_abort_the_pass(self):
        """Teardown is the half that costs money to skip."""
        hub = StubHub(demand={"gpu-a10": 1})
        ctrl, _ = build(hub, pools=self.pools(),
                        providers={"first": FakeProvider(create_raises=True),
                                   "second": FakeProvider()})
        ctrl.reconcile()          # must not raise


class DepartedPools(unittest.TestCase):
    def test_an_idle_machine_whose_pool_left_the_config_is_still_reaped(self):
        """Otherwise it is reaped by no rule at all while healthy and idle --
        billing forever, with nothing in the log about it."""
        hub = StubHub(demand={"gpu-a10": 0},
                      vnodes={"external-fake-x": ready(pool="retired")},
                      used={"external-fake-x": 0})
        ctrl, clock = build(hub, pools=(GPU,),
                            cfg=make_config(
                                pools=[GPU],
                                providers=[ProviderSpec(name="fake")],
                                scale={"max_machines": 99, "idle_s": 600}))
        provider = ctrl.providers["fake"]
        provider.machines["external-fake-x"] = Machine(
            name="external-fake-x", id="1", status="ACTIVE", created=clock(),
            ip="198.51.100.9")
        hub.credentials["external-fake-x"] = _creds("retired")

        ctrl.reconcile()
        self.assertEqual(provider.destroyed, [])
        clock.advance(700)
        ctrl.reconcile()
        self.assertEqual(provider.destroyed, ["external-fake-x"])

    def test_a_departed_pool_is_never_scaled_up_for(self):
        hub = StubHub(demand={"gpu-a10": 0, "retired": 5})
        ctrl, _ = build(hub, pools=(GPU,))
        ctrl.reconcile()
        self.assertEqual(ctrl.providers["fake"].created, [])

    def test_the_unknown_bucket_has_no_minimum(self):
        """A floor from a pool that no longer exists would pin machines alive."""
        self.assertEqual(
            Controller(StubHub(), StubLiqo(), {}, make_config())
            .pool_for(UNKNOWN_POOL).min_machines, 0)


class BlindProviders(unittest.TestCase):
    def test_a_provider_that_cannot_be_listed_does_not_orphan_its_machines(self):
        """One failed API call must not read as "the whole cloud vanished" and
        tear down every healthy peering on it."""
        hub = StubHub(demand={"gpu-a10": 0})
        liqo = StubLiqo(peered={"external-fake-a", "external-fake-b"})
        ctrl, clock = build(hub, pools=(GPU,),
                            providers={"fake": FakeProvider(list_raises=True)})
        ctrl.liqo = liqo
        for _ in range(5):
            ctrl.reconcile()
            clock.advance(600)
        self.assertEqual(liqo.torn_down, [])

    def test_a_reachable_provider_still_reaps_normally(self):
        """The skip is scoped to not knowing, not to any failure anywhere."""
        hub = StubHub(demand={"gpu-a10": 0})
        liqo = StubLiqo(peered={"external-fake-gone"})
        ctrl, clock = build(hub, pools=(GPU,))
        ctrl.liqo = liqo
        ctrl.reconcile()
        clock.advance(600)
        ctrl.reconcile()
        self.assertEqual(liqo.torn_down, ["external-fake-gone"])


def _creds(pool):
    from autoscaler import pki
    creds = pki.generate("external-fake-x")[1]
    creds.pool = pool
    return creds


if __name__ == "__main__":
    unittest.main()


class PerPoolReserve(unittest.TestCase):
    """What is held back on every machine.

    Per-pool because it is a fact about MACHINE SIZE: 2Gi is prudent on a 45GB
    box and most of a 4GB one.
    """

    def slice_for(self, pool, capacity):
        from autoscaler.liqo import Liqo
        cfg = make_config(pools=[pool])
        written = {}

        class Crd:
            def create_namespaced_custom_object(self, g, v, ns, plural, body):
                written.update(body["spec"]["resources"])

        liqo = Liqo.__new__(Liqo)
        liqo.cfg = cfg
        liqo.crd = Crd()
        liqo.create_slice("m", pool, capacity)
        return written

    def test_the_reserve_is_subtracted_from_what_is_advertised(self):
        pool = make_pool("cpu",
                         system_reserve=Resources({"cpu": "1", "memory": "2Gi"}),
                         daemonset_reserve=Resources({"cpu": "200m"}))
        got = self.slice_for(pool, Resources(
            {"cpu": "4", "memory": "8Gi", "pods": 10}))
        self.assertEqual(got["cpu"], "2800m")          # 4 - 1 - 0.2
        self.assertEqual(got["memory"], "6144Mi")      # 8Gi - 2Gi

    def test_every_dimension_is_advertised_not_just_cpu_and_memory(self):
        """A dimension left out is one the scheduler will oversubscribe."""
        pool = make_pool("gpu")
        got = self.slice_for(pool, Resources(
            {"cpu": "12", "memory": "45Gi", "nvidia.com/gpu": 2, "pods": 50}))
        self.assertEqual(got["nvidia.com/gpu"], "2")

    def test_a_slice_is_never_zero_or_negative(self):
        """Whatever the arithmetic says, the slice has to be usable."""
        pool = make_pool("absurd",
                         system_reserve=Resources({"cpu": "99", "memory": "99Gi"}))
        got = self.slice_for(pool, Resources({"cpu": "2", "memory": "4Gi"}))
        self.assertNotIn("cpu", got)   # clamped away entirely
        self.assertEqual(got["pods"], "50")

    def test_pods_defaults_when_the_node_does_not_report_it(self):
        got = self.slice_for(make_pool("p"), Resources({"cpu": "4"}))
        self.assertEqual(got["pods"], "50")


class EndToEndScaling(unittest.TestCase):
    """The behaviours the vector model exists for, through the whole loop."""

    def build_pool(self, shape, **pool_kw):
        provider = FakeProvider(capacity={"f": shape})
        pool = make_pool("gpu-a10", max_machines=99, **pool_kw)
        cfg = make_config(pools=[pool],
                          providers=[ProviderSpec(name="fake", batch_size=99)],
                          scale={"max_machines": 99})
        return provider, pool, cfg

    def rent(self, shape, pending, **pool_kw):
        provider, pool, cfg = self.build_pool(shape, **pool_kw)
        hub = StubHub(demand={"gpu-a10": pending})
        ctrl, _ = build(hub, providers={"fake": provider}, cfg=cfg)
        ctrl.reconcile()
        return provider.created

    def test_indivisible_pods_rent_one_machine_each(self):
        """Four 3-CPU pods against 4-CPU machines: 12 CPU of demand looks like
        three machines, but only one pod fits per machine."""
        shape = Resources({"cpu": "4", "pods": 10})
        pending = [(f"p{i}", Resources({"cpu": "3", "pods": 1})) for i in range(4)]
        self.assertEqual(len(self.rent(shape, pending)), 4)

    def test_memory_can_be_the_binding_dimension(self):
        shape = Resources({"cpu": "16", "memory": "8Gi", "pods": 10})
        pending = [(f"p{i}", Resources({"cpu": "1", "memory": "3Gi", "pods": 1}))
                   for i in range(4)]
        self.assertEqual(len(self.rent(shape, pending)), 2)

    def test_a_pod_no_machine_can_hold_rents_nothing(self):
        """Otherwise the pool rents to its ceiling for a pod none of them run."""
        shape = Resources({"nvidia.com/gpu": 1, "cpu": "12", "pods": 10})
        pending = [("huge", Resources({"nvidia.com/gpu": 8, "pods": 1}))]
        self.assertEqual(self.rent(shape, pending), [])

    def test_an_unmeasured_dimension_rents_one_to_find_out(self):
        """A GPU pod against a shape with no GPU entry means the flavor is not
        in the table, not that the machine has none."""
        shape = Resources({"cpu": "12", "memory": "45Gi", "pods": 10})
        pending = [("g", Resources({"nvidia.com/gpu": 1, "pods": 1}))]
        self.assertEqual(len(self.rent(shape, pending)), 1)

    def test_nothing_known_about_the_machine_rents_exactly_one(self):
        provider = FakeProvider(capacity={})
        provider.default_capacity = {}
        pool = make_pool("gpu-a10", max_machines=99)
        cfg = make_config(pools=[pool],
                          providers=[ProviderSpec(name="fake", batch_size=99)],
                          scale={"max_machines": 99})
        hub = StubHub(demand={"gpu-a10": [
            (f"p{i}", Resources({"cpu": "1", "pods": 1})) for i in range(20)]})
        ctrl, _ = build(hub, providers={"fake": provider}, cfg=cfg)
        ctrl.reconcile()
        self.assertEqual(len(provider.created), 1)

    def test_the_reserve_shrinks_what_a_machine_can_take(self):
        """Two 1-CPU pods fit a 2-CPU machine, but not once 1 CPU is held back
        for k3s and Liqo."""
        shape = Resources({"cpu": "2", "pods": 10})
        pending = [(f"p{i}", Resources({"cpu": "1", "pods": 1})) for i in range(2)]
        bare = self.rent(shape, pending)
        reserved = self.rent(shape, pending,
                             system_reserve=Resources({"cpu": "1"}))
        self.assertEqual((len(bare), len(reserved)), (1, 2))


class DaemonsetsDoNotHoldMachines(unittest.TestCase):
    def reap_after_idle(self, used):
        hub = StubHub(demand={"gpu-a10": 0},
                      vnodes={"external-fake-x": {
                          "ready": True, "since": 0.0,
                          "capacity": Resources({"cpu": "4", "pods": 10}),
                          "pool": "gpu-a10"}},
                      used={"external-fake-x": used})
        pool = make_pool("gpu-a10", max_machines=99)
        cfg = make_config(pools=[pool],
                          providers=[ProviderSpec(name="fake")],
                          scale={"max_machines": 99, "idle_s": 600})
        ctrl, clock = build(hub, cfg=cfg)
        provider = ctrl.providers["fake"]
        provider.machines["external-fake-x"] = Machine(
            name="external-fake-x", id="1", status="ACTIVE", created=clock(),
            ip="198.51.100.9")
        hub.credentials["external-fake-x"] = _creds("gpu-a10")
        ctrl.reconcile()
        clock.advance(700)
        ctrl.reconcile()
        return provider.destroyed

    def test_a_machine_holding_only_daemonsets_is_reaped(self):
        """used_on excludes them, so this reads as zero. If it did not, the
        machine would bill forever and nothing would say why."""
        self.assertEqual(self.reap_after_idle(Resources()), ["external-fake-x"])

    def test_a_machine_with_real_work_is_not(self):
        self.assertEqual(self.reap_after_idle(Resources({"cpu": "1", "pods": 1})), [])


class BootingMachinesAreSupply(unittest.TestCase):
    """The live failure: two pods, four machines.

    A booting machine has no pods yet, so its whole shape is free. Not counting
    it means every pass re-rents for demand already on its way, until the pool
    hits maxMachines.
    """

    def rent_with_booting(self, booting):
        shape = Resources({"cpu": "1200m", "memory": "2Gi", "pods": 50})
        provider = FakeProvider(capacity={"d2-4": shape})
        pool = make_pool("cpu-dryrun", max_machines=99)
        cfg = make_config(pools=[pool],
                          providers=[ProviderSpec(name="fake", batch_size=99)],
                          scale={"max_machines": 99})
        hub = StubHub(demand={"cpu-dryrun": [
            (f"c{i}", Resources({"cpu": "500m", "memory": "64Mi", "pods": 1}))
            for i in range(2)]})
        ctrl, clock = build(hub, providers={"fake": provider}, cfg=cfg)
        for i in range(booting):
            name = f"external-fake-b{i}"
            provider.machines[name] = Machine(
                name=name, id=str(i), status="BUILD", created=clock(),
                ip=None)
            hub.credentials[name] = _creds("cpu-dryrun")
        ctrl.reconcile()
        return len(provider.created)

    def test_from_cold_two_pods_rent_one_machine(self):
        self.assertEqual(self.rent_with_booting(0), 1)

    def test_a_machine_already_booting_absorbs_them(self):
        """It has not peered yet, so it holds nothing and its whole shape is
        free. Renting again here is what produced four machines for two pods."""
        self.assertEqual(self.rent_with_booting(1), 0)


class MixedFlavorsInOnePool(unittest.TestCase):
    """The live failure: adding d2-8 beside d2-4 and a 4Gi workload got
    "exceeds an entire machine" for every high pod, because the pool was sized
    on its first flavor alone and create() rented that flavor regardless.
    """

    SHAPES = {("cpu-dryrun", "fake"): {
        "d2-4": Resources({"cpu": 2 * 1000, "memory": 4 * 1000 ** 3}),
        "d2-8": Resources({"cpu": 4 * 1000, "memory": 8 * 1000 ** 3})}}

    def rent(self, small=0, big=0, shapes=None, booting=()):
        provider = FakeProvider()
        provider.placements = {"cpu-dryrun": FakePlacement()}
        pool = make_pool(
            "cpu-dryrun", max_machines=99,
            system_reserve=Resources({"cpu": "700m", "memory": "1536Mi"}),
            daemonset_reserve=Resources({"cpu": "100m", "memory": "128Mi"}))
        cfg = make_config(pools=[pool],
                          providers=[ProviderSpec(name="fake", batch_size=99)],
                          scale={"max_machines": 99})
        demand = {"cpu-dryrun":
                  [(f"s{i}", Resources({"cpu": "500m", "memory": "64Mi",
                                        "pods": 1})) for i in range(small)]
                  + [(f"h{i}", Resources({"cpu": "500m", "memory": "4096Mi",
                                          "pods": 1})) for i in range(big)]}
        hub = StubHub(demand=demand)
        ctrl, clock = build(hub, providers={"fake": provider}, cfg=cfg)
        ctrl._flavor_shapes = dict(
            self.SHAPES if shapes is None else shapes)
        for i, flavor in enumerate(booting):
            name = f"external-fake-b{i}"
            provider.machines[name] = Machine(
                name=name, id=str(i), status="BUILD", created=clock(), ip=None)
            creds = _creds("cpu-dryrun")
            creds.flavor = flavor
            hub.credentials[name] = creds
        ctrl.reconcile()
        return Counter(provider.created_flavors)

    def test_small_pods_take_the_cheap_flavor(self):
        self.assertEqual(self.rent(small=2), Counter({"d2-4": 1}))

    def test_a_pod_too_big_for_the_first_flavor_rents_the_bigger_one(self):
        """This is what returned "exceeds an entire machine" before."""
        self.assertEqual(self.rent(big=1), Counter({"d2-8": 1}))

    def test_both_workloads_share_the_bigger_machines(self):
        """A d2-8 opened for a high pod has room left, so the small pods ride
        along rather than renting a d2-4 as well."""
        self.assertEqual(self.rent(small=2, big=2), Counter({"d2-8": 2}))

    def test_with_only_the_small_flavor_it_is_genuinely_impossible(self):
        shapes = {("cpu-dryrun", "fake"): {
            "d2-4": Resources({"cpu": 2 * 1000, "memory": 4 * 1000 ** 3})}}
        self.assertEqual(self.rent(big=1, shapes=shapes), Counter())

    def test_a_booting_big_machine_absorbs_a_big_pod(self):
        """Sizing a booting machine as the pool's FIRST flavor is what made big
        pods rent one every pass: a d2-8 counted as a d2-4 leaves free space the
        pod does not fit."""
        self.assertEqual(self.rent(big=1, booting=("d2-8",)), Counter())

    def test_a_booting_small_machine_does_not(self):
        self.assertEqual(self.rent(big=1, booting=("d2-4",)),
                         Counter({"d2-8": 1}))
