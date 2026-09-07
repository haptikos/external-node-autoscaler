"""Pod -> pool matching.

A prediction of a decision the scheduler makes later, against declared labels
instead of a real node's. If the two disagree the operator rents a machine the
scheduler refuses to use. Hence a table rather than a few examples.
"""
import types
import unittest

from support import make_config          # noqa: F401  (sys.path bootstrap)

from autoscaler import pools
from autoscaler.pools import Pool, PoolProvider, Taint

OVH = (PoolProvider(name="ovh", priority=10),)

GPU = Pool(name="gpu-a10", node_labels={"byon/pool": "gpu-a10"},
           taints=(Taint("byon/pool", "gpu-a10", "NoSchedule"),),
           providers=OVH)
CPU = Pool(name="cpu-dryrun", node_labels={"byon/pool": "cpu-dryrun"},
           providers=OVH)


def pod(node_selector=None, tolerations=(), affinity=None):
    """Just the scheduling half of a V1Pod, shaped as the client returns it."""
    return types.SimpleNamespace(
        metadata=types.SimpleNamespace(name="p", namespace="inference"),
        spec=types.SimpleNamespace(
            node_selector=node_selector,
            tolerations=list(tolerations),
            affinity=affinity,
            containers=[]))


def toleration(key=None, operator=None, value=None, effect=None):
    return types.SimpleNamespace(key=key, operator=operator, value=value,
                                 effect=effect)


def expr(key, operator, values=None):
    return types.SimpleNamespace(key=key, operator=operator, values=values)


def affinity(*terms):
    """requiredDuringSchedulingIgnoredDuringExecution with these terms."""
    return types.SimpleNamespace(node_affinity=types.SimpleNamespace(
        required_during_scheduling_ignored_during_execution=(
            types.SimpleNamespace(node_selector_terms=[
                types.SimpleNamespace(match_expressions=list(t),
                                      match_fields=None) for t in terms]))))


#: What a pod needs to be placed on ANY virtual node — Liqo's own taint.
REMOTE = (toleration(key="virtual-node.liqo.io/not-allowed", operator="Exists",
                     effect="NoExecute"),)


class TheRegressionsThatMatter(unittest.TestCase):
    """Two behaviours whose breakage would be silent and expensive."""

    def test_todays_inference_pod_still_matches_an_untainted_pool(self):
        """The demo workload selects `liqo.io/type: virtual-node` and nothing
        else. Liqo stamps that label, not us, so a pool must inherit it or every
        existing workload becomes unroutable the day pools land."""
        p = pod(node_selector={"liqo.io/type": "virtual-node"},
                tolerations=REMOTE)
        self.assertIs(pools.match(p, [CPU]), CPU)

    def test_a_pool_agnostic_pod_does_not_land_on_a_tainted_pool(self):
        """The taint is what stops an unselective pod from renting an A10. With
        only the GPU pool declared this pod is unroutable, which is the intended
        answer -- renting is worse."""
        p = pod(node_selector={"liqo.io/type": "virtual-node"},
                tolerations=REMOTE)
        self.assertIsNone(pools.match(p, [GPU]))


class NodeSelector(unittest.TestCase):
    def test_selecting_a_pool_by_name(self):
        p = pod(node_selector={"byon/pool": "cpu-dryrun"},
                tolerations=REMOTE)
        self.assertIs(pools.match(p, [GPU, CPU]), CPU)

    def test_a_wrong_value_matches_nothing(self):
        p = pod(node_selector={"byon/pool": "gpu-h100"},
                tolerations=REMOTE)
        self.assertIsNone(pools.match(p, [GPU, CPU]))

    def test_every_key_is_required(self):
        p = pod(node_selector={"byon/pool": "cpu-dryrun",
                               "byon/zone": "gra"},
                tolerations=REMOTE)
        self.assertIsNone(pools.match(p, [CPU]))

    def test_no_selector_matches_the_first_untainted_pool(self):
        p = pod(tolerations=REMOTE)
        self.assertIs(pools.match(p, [GPU, CPU]), CPU)


class Tolerations(unittest.TestCase):
    def test_a_pod_without_the_liqo_toleration_matches_nothing(self):
        """Liqo's webhook injects it, but only once the namespace is labelled
        scheduling-enabled -- which is why the inference chart sets it itself."""
        self.assertIsNone(pools.match(pod(), [CPU]))

    def test_tolerating_the_pool_taint_by_value(self):
        p = pod(node_selector={"byon/pool": "gpu-a10"},
                tolerations=REMOTE + (toleration(
                    key="byon/pool", operator="Equal", value="gpu-a10",
                    effect="NoSchedule"),))
        self.assertIs(pools.match(p, [GPU]), GPU)

    def test_a_wrong_toleration_value_does_not_tolerate(self):
        p = pod(node_selector={"byon/pool": "gpu-a10"},
                tolerations=REMOTE + (toleration(
                    key="byon/pool", operator="Equal", value="gpu-h100",
                    effect="NoSchedule"),))
        self.assertIsNone(pools.match(p, [GPU]))

    def test_an_empty_effect_tolerates_every_effect(self):
        p = pod(tolerations=(toleration(operator="Exists"),))
        self.assertIs(pools.match(p, [GPU]), GPU)

    def test_a_mismatched_effect_does_not_tolerate(self):
        p = pod(node_selector={"byon/pool": "gpu-a10"},
                tolerations=REMOTE + (toleration(
                    key="byon/pool", operator="Exists",
                    effect="NoExecute"),))
        self.assertIsNone(pools.match(p, [GPU]))

    def test_operator_defaults_to_equal_when_unset(self):
        p = pod(tolerations=(toleration(key="virtual-node.liqo.io/not-allowed",
                                        value="true", effect="NoExecute"),))
        self.assertIs(pools.match(p, [CPU]), CPU)


class NodeAffinity(unittest.TestCase):
    def match(self, *terms, selector=None):
        return pools.match(
            pod(node_selector=selector, tolerations=REMOTE,
                affinity=affinity(*terms)),
            [GPU, CPU])

    def test_in_operator(self):
        self.assertIs(
            self.match([expr("byon/pool", "In", ["cpu-dryrun"])]), CPU)

    def test_in_with_several_values_matches_the_first_pool_that_qualifies(self):
        p = pod(tolerations=REMOTE, affinity=affinity(
            [expr("byon/pool", "In", ["gpu-a10", "cpu-dryrun"])]))
        # GPU qualifies on labels but its taint is untolerated, so CPU wins.
        self.assertIs(pools.match(p, [GPU, CPU]), CPU)

    def test_not_in(self):
        self.assertIs(
            self.match([expr("byon/pool", "NotIn", ["gpu-a10"])]), CPU)

    def test_exists(self):
        self.assertIs(self.match([expr("byon/zone", "Exists")]), None)

    def test_does_not_exist(self):
        self.assertIs(
            self.match([expr("byon/zone", "DoesNotExist")]), CPU)

    def test_expressions_within_a_term_are_anded(self):
        self.assertIsNone(self.match([
            expr("byon/pool", "In", ["cpu-dryrun"]),
            expr("byon/zone", "Exists")]))

    def test_terms_are_ored(self):
        self.assertIs(self.match(
            [expr("byon/zone", "Exists")],
            [expr("byon/pool", "In", ["cpu-dryrun"])]), CPU)

    def test_an_unsupported_operator_matches_nothing(self):
        """Gt/Lt compare integers and cannot select a pool. Refusing to match
        reports the pod unroutable rather than guessing a pool for it."""
        self.assertIsNone(self.match([expr("byon/rank", "Gt", ["3"])]))

    def test_match_fields_matches_nothing(self):
        """matchFields selects on metadata.name -- there is no node yet."""
        p = pod(tolerations=REMOTE, affinity=types.SimpleNamespace(
            node_affinity=types.SimpleNamespace(
                required_during_scheduling_ignored_during_execution=(
                    types.SimpleNamespace(node_selector_terms=[
                        types.SimpleNamespace(
                            match_expressions=None,
                            match_fields=[expr("metadata.name", "In", ["x"])])
                    ])))))
        self.assertIsNone(pools.match(p, [CPU]))

    def test_preferred_affinity_is_not_a_constraint(self):
        """Treating a preference as a requirement would refuse to rent a machine
        the scheduler would in fact have used."""
        p = pod(tolerations=REMOTE, affinity=types.SimpleNamespace(
            node_affinity=types.SimpleNamespace(
                required_during_scheduling_ignored_during_execution=None,
                preferred_during_scheduling_ignored_during_execution=["x"])))
        self.assertIs(pools.match(p, [CPU]), CPU)


class Ordering(unittest.TestCase):
    def test_declaration_order_settles_a_pod_that_fits_both(self):
        a = Pool(name="a", node_labels={"k": "v"}, providers=OVH)
        b = Pool(name="b", node_labels={"k": "v"}, providers=OVH)
        p = pod(node_selector={"k": "v"}, tolerations=REMOTE)
        self.assertIs(pools.match(p, [a, b]), a)
        self.assertIs(pools.match(p, [b, a]), b)

    def test_labels_beat_order(self):
        """Order is the tiebreak, never the rule: a later pool whose labels
        actually match wins over an earlier one whose labels do not."""
        p = pod(node_selector={"byon/pool": "cpu-dryrun"},
                tolerations=REMOTE)
        self.assertIs(pools.match(p, [GPU, CPU]), CPU)


class Validation(unittest.TestCase):
    def validate(self, pool, providers=("ovh",)):
        return pools.validate([pool], set(providers))

    def test_a_pool_naming_an_undeclared_provider_is_rejected(self):
        with self.assertRaises(ValueError) as caught:
            self.validate(Pool(name="p", providers=(PoolProvider("hetzner"),)))
        self.assertIn("hetzner", str(caught.exception))

    def test_a_pool_with_no_providers_is_rejected(self):
        with self.assertRaises(ValueError) as caught:
            self.validate(Pool(name="p"))
        self.assertIn("no providers", str(caught.exception))

    def test_min_above_max_is_rejected(self):
        """It would rent to reach the floor and reap straight back down."""
        with self.assertRaises(ValueError) as caught:
            self.validate(Pool(name="p", min_machines=3, max_machines=1,
                               providers=OVH))
        self.assertIn("minMachines", str(caught.exception))

    def test_a_bad_taint_effect_is_rejected(self):
        with self.assertRaises(ValueError) as caught:
            self.validate(Pool(name="p", taints=(Taint("k", "v", "NoSchedul"),),
                               providers=OVH))
        self.assertIn("NoSchedul", str(caught.exception))

    def test_duplicate_pool_names_are_rejected(self):
        with self.assertRaises(ValueError):
            pools.validate([CPU, CPU], {"ovh"})

    def test_no_pools_at_all_is_rejected(self):
        with self.assertRaises(ValueError):
            pools.validate([], {"ovh"})

    def test_a_duplicate_untainted_pool_is_warned_about(self):
        """Two pools with the same labels and no taint on the first: the second
        can never be selected. It simply never scales, with nothing in the log
        connecting that to pool order."""
        first = Pool(name="first", node_labels={"byon/pool": "a"},
                     providers=OVH)
        second = Pool(name="second", node_labels={"byon/pool": "a"},
                      providers=OVH)
        with self.assertLogs("pools", level="WARNING") as caught:
            pools.validate([first, second], {"ovh"})
        self.assertIn("can never be selected", "\n".join(caught.output))

    def test_a_more_specific_pool_shadows_a_less_specific_one_below_it(self):
        """Every selector `narrow` can satisfy, `wide` satisfies too -- because
        wide carries a superset of the labels."""
        wide = Pool(name="wide",
                    node_labels={"byon/pool": "a", "byon/zone": "gra"},
                    providers=OVH)
        narrow = Pool(name="narrow", node_labels={"byon/pool": "a"},
                      providers=OVH)
        with self.assertLogs("pools", level="WARNING") as caught:
            pools.validate([wide, narrow], {"ovh"})
        self.assertIn("narrow", "\n".join(caught.output))

    def test_a_label_less_catch_all_does_not_shadow_a_labelled_pool(self):
        """The tempting false positive: a pod that selects gpu-a10 does NOT
        match a pool with no labels, so the labelled pool below is reachable."""
        catch_all = Pool(name="any", providers=OVH)
        with self.assertNoLogs("pools", level="WARNING"):
            pools.validate([catch_all, GPU], {"ovh"})

    def test_a_taint_on_the_earlier_pool_is_not_shadowing(self):
        with self.assertNoLogs("pools", level="WARNING"):
            pools.validate([GPU, CPU], {"ovh"})


if __name__ == "__main__":
    unittest.main()
