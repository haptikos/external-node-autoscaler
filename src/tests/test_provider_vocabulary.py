"""The keys an operator fills in must mean the same thing in every provider.

This exists because they diverged once: OVH took `flavors` and Vultr took
`plans`, each mirroring its own cloud's word. Nothing broke — both parsed fine —
but a pool holding two providers then spelled the same idea two ways in adjacent
blocks, and moving a machine list from one to the other silently rented the
default instead.

Provider-native vocabulary belongs inside provider.py, at the API call. It does
not belong in values.

Driven by providers.available() rather than a hand-written list, so a provider
added later is covered without editing this file.
"""
import unittest

from support import make_config          # noqa: F401  (sys.path bootstrap)

from autoscaler import providers
from autoscaler.providers.base import Provider, ProviderConfigError

#: What every placement block is expected to accept. `gpu` and `region` are here
#: for the same reason as `flavors`: they are questions every cloud answers.
COMMON_KEYS = ("flavors", "gpu", "region")


#: `fake` rents nothing and is never named in a values file — its placement
#: carries a capacity table the controller tests set by hand. Holding a test
#: double to the operator-facing vocabulary would be churn with no reader.
NOT_AN_OPERATOR_CHOICE = ("fake",)


def placement_providers():
    """Every provider that a pool can actually be pointed at.

    One that does not implement parse_placement (the base raises) is not a
    divergence — it simply cannot be used in a pool, which the loader reports.
    """
    out = {}
    for name in providers.available():
        if name in NOT_AN_OPERATOR_CHOICE:
            continue
        try:
            module = __import__(f"autoscaler.providers.{name}", fromlist=["PROVIDER"])
        except ImportError:                             # optional dependency
            continue
        cls = module.PROVIDER
        if cls.parse_placement.__func__ is Provider.parse_placement.__func__:
            continue
        out[name] = cls
    return out


class VocabularyTest(unittest.TestCase):
    def test_there_is_at_least_one_provider_to_check(self):
        """Otherwise every assertion below passes vacuously."""
        self.assertTrue(placement_providers())

    def test_every_provider_takes_flavors(self):
        for name, cls in placement_providers().items():
            with self.subTest(name):
                placement = cls.parse_placement({"flavors": ["something"]})
                self.assertEqual(tuple(placement.flavors), ("something",),
                                 f"{name} accepted `flavors` but did not store "
                                 f"it as .flavors")

    def test_every_provider_accepts_the_common_keys(self):
        """A key one provider takes and another rejects is the same trap in a
        smaller form: the block looks portable and is not."""
        for name, cls in placement_providers().items():
            for key in COMMON_KEYS:
                with self.subTest(f"{name}.{key}"):
                    cfg = {"flavors": ["something"]}
                    cfg[key] = cfg.get(key, {"gpu": False, "region": "x",
                                             "flavors": ["something"]}[key])
                    try:
                        cls.parse_placement(cfg)
                    except ProviderConfigError as e:
                        self.fail(f"{name} rejects the common key {key!r}: {e}")

    def test_a_providers_own_word_is_not_silently_accepted(self):
        """The failure mode that motivated this file. A provider still taking
        its cloud's own spelling would let both work, and the divergence would
        creep back a values file at a time."""
        for name, cls in placement_providers().items():
            with self.subTest(name):
                with self.assertRaises(
                        ProviderConfigError,
                        msg=f"{name} accepted `plans` as well as `flavors`"):
                    cls.parse_placement({"plans": ["something"]})


if __name__ == "__main__":
    unittest.main()
