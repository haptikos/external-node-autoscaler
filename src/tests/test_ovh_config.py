"""The OVH config parsers: the only thing between a typo in Helm values and a
machine rented in the wrong region on the wrong flavor, because the chart passes
both blocks through verbatim and never reads a key.

Two parsers, split the way the fleet file is: OvhConfig is the account (one
project, one set of credentials), OvhPlacement is what one pool rents from it.
"""
import pathlib
import unittest

from support import make_config          # noqa: F401  (sys.path bootstrap)

from autoscaler.providers import cloudinit
from autoscaler.providers.base import ProviderConfigError
from autoscaler.providers.ovh.config import OvhConfig, OvhPlacement

#: Shape-accurate and obviously fake. OVH project ids are 32 hex chars and
#: identify an account, so a real one does not belong in a public repo.
PROJECT = "deadbeefdeadbeefdeadbeefdeadbeef"


class ConfigTest(unittest.TestCase):
    def parse(self, **over):
        return OvhConfig.from_dict({"project": PROJECT, **over})

    def test_project_is_required(self):
        with self.assertRaises(ProviderConfigError):
            OvhConfig.from_dict({})

    def test_region_defaults_and_is_overridable(self):
        self.assertEqual(self.parse().region, "GRA11")
        self.assertEqual(self.parse(region="SBG5").region, "SBG5")

    def test_an_unknown_key_is_loud(self):
        """A typo would otherwise leave the default silently in force."""
        with self.assertRaises(ProviderConfigError) as caught:
            self.parse(Region="SBG5")           # wrong case
        self.assertIn("Region", str(caught.exception))

    def test_a_placement_key_here_is_rejected(self):
        """flavors belongs to a POOL, not to the account. Silently accepting it
        here would mean a pool's flavor list was never read and every pool
        rented the same thing."""
        with self.assertRaises(ProviderConfigError) as caught:
            self.parse(flavors=["a10-45"])
        self.assertIn("flavors", str(caught.exception))

    def test_the_default_script_is_the_shared_one(self):
        """Not a copy under ovh/: every raw-VM provider boots the same script,
        and two copies of it drift."""
        cfg = self.parse()
        self.assertEqual(cfg.provision_script, cloudinit.DEFAULT_SCRIPT)
        self.assertTrue(pathlib.Path(cfg.provision_script).exists())

    def test_a_relative_provision_script_resolves_against_providers(self):
        cfg = self.parse(provisionScript="provision.sh")
        self.assertEqual(cfg.provision_script, cloudinit.DEFAULT_SCRIPT)

    def test_an_absolute_provision_script_is_honoured_as_given(self):
        """The hot-patch escape hatch: mount a ConfigMap over it."""
        self.assertEqual(
            self.parse(provisionScript="/config/provision.sh").provision_script,
            "/config/provision.sh")


class PlacementTest(unittest.TestCase):
    def parse(self, **over):
        """`flavors` has no default, so supply one unless the test is about
        exactly that."""
        over.setdefault("flavors", ["d2-4"])
        return OvhPlacement.from_dict(over)

    def test_flavors_have_no_default(self):
        """They used to default to ("a10-45", "rtx5000-28"), so a placement that
        simply forgot the key rented a GPU box. There is no safe guess here: the
        only flavor list that cannot surprise the bill is the one written down."""
        with self.assertRaises(ProviderConfigError):
            OvhPlacement.from_dict({})

    def test_flavors_are_read_in_order(self):
        self.assertEqual(self.parse(flavors=["d2-8", "d2-4"]).flavors,
                         ("d2-8", "d2-4"))

    def test_a_comma_string_is_tolerated(self):
        """A bare string would otherwise iterate character by character and try
        to rent a flavor called "a"."""
        self.assertEqual(self.parse(flavors="d2-8, d2-4").flavors,
                         ("d2-8", "d2-4"))

    def test_empty_flavors_are_rejected(self):
        with self.assertRaises(ProviderConfigError):
            self.parse(flavors=[])

    def test_gpu_defaults_true_and_is_read(self):
        self.assertIs(self.parse().gpu, True)
        self.assertIs(self.parse(gpu=False).gpu, False)

    def test_units_per_machine_is_gone_and_rejected(self):
        """Machine size is measured now, not declared. Silently ignoring the
        old key would leave someone believing it still sizes their pool."""
        with self.assertRaises(ProviderConfigError) as caught:
            self.parse(unitsPerMachine=2)
        self.assertIn("unitsPerMachine", str(caught.exception))

    def test_region_override_is_optional(self):
        self.assertIsNone(self.parse().region)
        self.assertEqual(self.parse(region="SBG5").region, "SBG5")

    def test_an_unknown_key_is_loud(self):
        with self.assertRaises(ProviderConfigError) as caught:
            self.parse(flavour=["d2-4"])          # British spelling
        self.assertIn("flavour", str(caught.exception))

    def test_an_account_key_here_is_rejected(self):
        """`project` is the account's, not a pool's. Accepting it per pool would
        imply pools can span projects, which credentials cannot."""
        with self.assertRaises(ProviderConfigError) as caught:
            self.parse(project=PROJECT)
        self.assertIn("project", str(caught.exception))

    def test_a_pool_may_list_flavors_that_do_not_match_its_name(self):
        """Deliberately not validated. A pool called gpu-a10 listing d2-8 as a
        fallback is the operator's call; naming pools is their job."""
        self.assertEqual(self.parse(flavors=["a10-45", "d2-8"]).flavors,
                         ("a10-45", "d2-8"))


if __name__ == "__main__":
    unittest.main()
