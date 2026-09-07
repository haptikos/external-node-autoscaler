"""How many machines a set of pending pods needs, and of which flavor.

Two facts drive this module, both learned the expensive way on a live cluster:
summed demand divided by machine size is wrong for pods that cannot be split,
and a pool listing several flavors must serve each pod with the first one that
holds it.
"""
import unittest

from support import make_config          # noqa: F401  (sys.path bootstrap)

from autoscaler.packing import Shape, plan
from autoscaler.resources import Resources

NODE = Resources({"cpu": "4", "memory": "8Gi", "pods": 10})

# The real numbers from the OVH dry-run pool, after reserves.
D2_4 = Shape("ovh", "d2-4", Resources({"cpu": "1200m", "memory": "2150Mi",
                                       "pods": 50}))
D2_8 = Shape("ovh", "d2-8", Resources({"cpu": "3200m", "memory": "5965Mi",
                                       "pods": 50}))


def one(vector):
    """A single-flavor pool."""
    return [Shape("fake", "only", vector)]


def pods(n, **req):
    return [(f"p{i}", Resources({**req, "pods": 1})) for i in range(n)]


def flavors(p):
    return [b.shape.flavor for b in p.bins]


class Indivisible(unittest.TestCase):
    def test_four_three_cpu_pods_need_four_four_cpu_nodes(self):
        """12 CPU of demand against 4-CPU nodes looks like 3 machines. Only one
        pod fits per node, so it is 4."""
        self.assertEqual(len(plan(pods(4, cpu="3"), [], one(NODE)).bins), 4)

    def test_pods_that_do_divide_evenly_still_do(self):
        self.assertEqual(len(plan(pods(4, cpu="2"), [], one(NODE)).bins), 2)

    def test_no_pods_no_machines(self):
        self.assertEqual(len(plan([], [], one(NODE)).bins), 0)


class ExistingCapacityFirst(unittest.TestCase):
    def test_a_pod_that_fits_existing_headroom_rents_nothing(self):
        free = [Resources({"cpu": "2", "memory": "4Gi", "pods": 5})]
        p = plan(pods(1, cpu="1"), free, one(NODE))
        self.assertEqual((len(p.bins), p.placed_existing), (0, 1))

    def test_headroom_is_consumed_not_reused(self):
        free = [Resources({"cpu": "1", "memory": "4Gi", "pods": 5})]
        p = plan(pods(2, cpu="1"), free, one(NODE))
        self.assertEqual((p.placed_existing, len(p.bins)), (1, 1))

    def test_heterogeneous_existing_nodes_use_their_own_free_space(self):
        """A pool can hold machines of different sizes; each is measured by what
        it actually has left, not by any pool-wide estimate."""
        free = [Resources({"cpu": "1", "memory": "2Gi", "pods": 5}),
                Resources({"cpu": "8", "memory": "16Gi", "pods": 5})]
        p = plan(pods(2, cpu="3"), free, one(NODE))
        self.assertEqual((p.placed_existing, len(p.bins)), (2, 0))

    def test_pod_capacity_is_a_real_dimension(self):
        """What used to be "slots"."""
        free = [Resources({"cpu": "4", "memory": "8Gi"})]     # no pods -> 0
        p = plan(pods(1, cpu="100m"), free, one(NODE))
        self.assertEqual((p.placed_existing, len(p.bins)), (0, 1))


class FlavorChoice(unittest.TestCase):
    """The live failure: a 4Gi pod in a pool listing [d2-4, d2-8] was declared
    impossible, because the pool was sized on its first flavor alone."""

    SMALL = ("s", Resources({"cpu": "500m", "memory": "64Mi", "pods": 1}))
    BIG = ("b", Resources({"cpu": "500m", "memory": "4096Mi", "pods": 1}))

    def test_a_small_pod_takes_the_first_flavor(self):
        self.assertEqual(flavors(plan([self.SMALL], [], [D2_4, D2_8])), ["d2-4"])

    def test_a_pod_too_big_for_the_first_flavor_takes_the_next(self):
        self.assertEqual(flavors(plan([self.BIG], [], [D2_4, D2_8])), ["d2-8"])

    def test_it_is_not_condemned_just_because_the_first_flavor_is_too_small(self):
        p = plan([self.BIG], [], [D2_4, D2_8])
        self.assertEqual(p.never_fits, [])

    def test_with_only_the_small_flavor_it_genuinely_cannot_run(self):
        p = plan([self.BIG], [], [D2_4])
        self.assertEqual([r for r, _ in p.never_fits], ["b"])
        self.assertEqual(len(p.bins), 0)

    def test_declaration_order_is_the_preference(self):
        """Both hold the pod; the pool's order decides, usually cheapest first."""
        self.assertEqual(flavors(plan([self.SMALL], [], [D2_8, D2_4])), ["d2-8"])

    def test_small_pods_ride_along_in_a_big_bin(self):
        """A machine opened for a big pod has room left; using it beats renting
        a second machine for the small ones."""
        p = plan([self.BIG, self.SMALL], [], [D2_4, D2_8])
        self.assertEqual(flavors(p), ["d2-8"])

    def test_two_big_pods_need_two_machines(self):
        """4096Mi each against 5965Mi schedulable: they do not share."""
        big2 = [("b1", self.BIG[1]), ("b2", self.BIG[1])]
        self.assertEqual(flavors(plan(big2, [], [D2_4, D2_8])),
                         ["d2-8", "d2-8"])


class NeverFits(unittest.TestCase):
    def test_a_pod_bigger_than_every_flavor_rents_nothing(self):
        big = [("huge", Resources({"nvidia.com/gpu": 8, "pods": 1}))]
        p = plan(big, [], one(Resources({"nvidia.com/gpu": 1, "cpu": "4",
                                         "pods": 10})))
        self.assertEqual(len(p.bins), 0)
        self.assertEqual([r for r, _ in p.never_fits], ["huge"])

    def test_it_does_not_block_the_pods_that_do_fit(self):
        mixed = [("huge", Resources({"cpu": "99", "pods": 1}))] + pods(2, cpu="2")
        p = plan(mixed, [], one(NODE))
        self.assertEqual((len(p.bins), len(p.never_fits)), (1, 1))

    def test_an_unmeasured_dimension_is_not_a_refusal(self):
        """A GPU pod against a shape with no GPU entry means the flavor is not
        in the table, not that the machine has none."""
        gpu = [("g", Resources({"nvidia.com/gpu": 1, "pods": 1}))]
        p = plan(gpu, [], one(Resources({"cpu": "4", "memory": "8Gi",
                                         "pods": 10})))
        self.assertEqual((p.never_fits, len(p.bins)), ([], 1))


class UnmeasuredVersusExhausted(unittest.TestCase):
    """Two things that both look like "absent", because Resources drops zeroes,
    and which must behave in opposite ways.

    This cost four machines for two pods on a live cluster: the OVH memory unit
    was wrong, memory clamped out of the shape, and a dimension the shape never
    measured was read as a hard zero. No pod fitted any bin, each opened its
    own, booting machines were never counted, and the pool rented to its ceiling.
    """

    def test_a_dimension_the_shape_never_measured_is_ignored(self):
        p = plan(pods(4, cpu="100m", memory="64Mi"), [],
                 one(Resources({"cpu": "4"})))
        self.assertEqual(len(p.bins), 1)

    def test_a_dimension_the_bin_exhausted_still_blocks(self):
        p = plan(pods(4, cpu="1"), [], one(Resources({"cpu": "2", "pods": 50})))
        self.assertEqual(len(p.bins), 2)


class UnknownShape(unittest.TestCase):
    def test_nothing_known_rents_exactly_one(self):
        """Under-renting costs a boot cycle; over-renting costs money until the
        idle timer notices."""
        self.assertEqual(len(plan(pods(20, cpu="1"), [], []).bins), 1)


class Ordering(unittest.TestCase):
    def test_the_awkward_pod_is_placed_first(self):
        mixed = [("small", Resources({"cpu": "1", "pods": 1})),
                 ("big", Resources({"cpu": "3", "pods": 1})),
                 ("small2", Resources({"cpu": "1", "pods": 1}))]
        self.assertEqual(len(plan(mixed, [], one(NODE)).bins), 2)

    def test_multi_dimensional_pressure_picks_the_binding_resource(self):
        p = plan(pods(4, cpu="500m", memory="3Gi"), [], one(NODE))
        self.assertEqual(len(p.bins), 2)


class TheDryRunShape(unittest.TestCase):
    def test_two_five_hundred_milli_pods_fill_one_d2_4(self):
        self.assertEqual(
            len(plan(pods(2, cpu="500m", memory="64Mi"), [], [D2_4]).bins), 1)

    def test_a_third_rents_a_second(self):
        self.assertEqual(
            len(plan(pods(3, cpu="500m", memory="64Mi"), [], [D2_4]).bins), 2)


if __name__ == "__main__":
    unittest.main()
