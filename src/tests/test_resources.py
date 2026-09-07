"""Resource vectors: parsing, algebra, and what a missing dimension means.

Everything downstream compares these, so a unit mix-up here is invisible until
the loop rents the wrong number of machines.
"""
import types
import unittest

from support import make_config          # noqa: F401  (sys.path bootstrap)

from autoscaler.resources import Resources, pod_requests


def container(requests=None):
    return types.SimpleNamespace(
        resources=types.SimpleNamespace(requests=requests or {}))


def pod(containers=(), init=(), overhead=None):
    return types.SimpleNamespace(spec=types.SimpleNamespace(
        containers=list(containers), init_containers=list(init),
        overhead=overhead))


class Parsing(unittest.TestCase):
    def test_cpu_is_millicores(self):
        self.assertEqual(Resources({"cpu": "2"})["cpu"], 2000)
        self.assertEqual(Resources({"cpu": "1500m"})["cpu"], 1500)
        self.assertEqual(Resources({"cpu": "100m"})["cpu"], 100)

    def test_memory_is_bytes(self):
        self.assertEqual(Resources({"memory": "1Gi"})["memory"], 1024 ** 3)
        self.assertEqual(Resources({"memory": "64Mi"})["memory"], 64 * 1024 ** 2)
        # Decimal suffixes are NOT binary ones; 500M is not 500Mi.
        self.assertEqual(Resources({"memory": "500M"})["memory"], 500_000_000)

    def test_extended_resources_stay_whole(self):
        self.assertEqual(Resources({"nvidia.com/gpu": 4})["nvidia.com/gpu"], 4)

    def test_zero_values_are_dropped(self):
        self.assertNotIn("cpu", Resources({"cpu": "0", "memory": "1Gi"}))

    def test_ints_pass_through_uninterpreted(self):
        """Already-canonical values must not be parsed a second time."""
        self.assertEqual(Resources({"cpu": 1500})["cpu"], 1500)


class Algebra(unittest.TestCase):
    def test_add_and_subtract(self):
        a = Resources({"cpu": "2", "memory": "4Gi"})
        b = Resources({"cpu": "500m"})
        self.assertEqual((a + b)["cpu"], 2500)
        self.assertEqual((a - b)["cpu"], 1500)

    def test_subtraction_may_go_negative_until_clamped(self):
        """Clamping is the caller's decision: oversubscription should be
        visible, not silently absorbed."""
        r = Resources({"cpu": "1"}) - Resources({"cpu": "3"})
        self.assertEqual(r["cpu"], -2000)
        self.assertTrue(r.clamp_zero().is_zero())

    def test_is_zero(self):
        self.assertTrue(Resources().is_zero())
        self.assertTrue(Resources({"cpu": "0"}).is_zero())
        self.assertFalse(Resources({"pods": 1}).is_zero())


class Fitting(unittest.TestCase):
    def test_fits_when_every_dimension_fits(self):
        self.assertTrue(Resources({"cpu": "1"}).fits_in(
            Resources({"cpu": "2", "memory": "1Gi"})))

    def test_does_not_fit_when_one_dimension_exceeds(self):
        self.assertFalse(Resources({"cpu": "1", "memory": "8Gi"}).fits_in(
            Resources({"cpu": "2", "memory": "1Gi"})))

    def test_a_dimension_the_capacity_never_mentions_is_unknown(self):
        """An unmeasured GPU count is ignorance, not a node with zero GPUs.
        Treating it as zero would refuse to rent for every GPU pod in a pool
        whose flavor is not in the table."""
        gpu_pod = Resources({"nvidia.com/gpu": 1})
        cpu_only = Resources({"cpu": "4"})
        self.assertTrue(gpu_pod.fits_in(cpu_only))

    def test_known_only_false_treats_absent_as_zero(self):
        """An observed node vector IS complete, so absent really is zero."""
        gpu_pod = Resources({"nvidia.com/gpu": 1})
        self.assertFalse(gpu_pod.fits_in(Resources({"cpu": "4"}),
                                         known_only=False))

    def test_dominant_ratio_ranks_by_the_tightest_dimension(self):
        node = Resources({"cpu": "4", "nvidia.com/gpu": 1})
        gpu = Resources({"cpu": "100m", "nvidia.com/gpu": 1})
        cpu = Resources({"cpu": "2"})
        self.assertGreater(gpu.dominant_ratio(node), cpu.dominant_ratio(node))


class PodRequests(unittest.TestCase):
    def test_containers_are_summed(self):
        r = pod_requests(pod([container({"cpu": "1"}), container({"cpu": "500m"})]))
        self.assertEqual(r["cpu"], 1500)

    def test_every_pod_costs_one_pod_slot(self):
        self.assertEqual(pod_requests(pod([container()]))["pods"], 1)

    def test_a_heavy_init_container_sets_the_floor(self):
        """Init containers run to completion before the app starts, so the pod's
        floor is the LARGER of the two. Summing app containers alone under-sizes
        it, and that pod is exactly the one that will not fit."""
        r = pod_requests(pod(containers=[container({"cpu": "500m"})],
                             init=[container({"cpu": "4"})]))
        self.assertEqual(r["cpu"], 4000)

    def test_a_light_init_container_does_not(self):
        r = pod_requests(pod(containers=[container({"cpu": "4"})],
                             init=[container({"cpu": "500m"})]))
        self.assertEqual(r["cpu"], 4000)

    def test_init_containers_are_maxed_not_summed(self):
        """They run one at a time."""
        r = pod_requests(pod(containers=[container()],
                             init=[container({"cpu": "2"}),
                                   container({"cpu": "3"})]))
        self.assertEqual(r["cpu"], 3000)

    def test_overhead_is_added(self):
        r = pod_requests(pod(containers=[container({"cpu": "1"})],
                             overhead={"cpu": "250m"}))
        self.assertEqual(r["cpu"], 1250)

    def test_a_pod_requesting_nothing_still_needs_a_slot(self):
        self.assertEqual(pod_requests(pod([container()]))._d, {"pods": 1})


if __name__ == "__main__":
    unittest.main()


class TheUnitTrap(unittest.TestCase):
    """A bare int is ALREADY canonical. This bit the OVH provider on its first
    boundary: handing Resources vcpus=12 meant 12 millicores, and the pool read
    a thousand times smaller than the machine actually was."""

    def test_an_int_cpu_is_millicores_not_cores(self):
        self.assertEqual(Resources({"cpu": 12})["cpu"], 12)
        self.assertEqual(Resources({"cpu": "12"})["cpu"], 12000)

    def test_the_two_spellings_of_twelve_cores_agree(self):
        self.assertEqual(Resources({"cpu": 12 * 1000}), Resources({"cpu": "12"}))
