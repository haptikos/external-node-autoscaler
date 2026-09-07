"""Vultr's config parsing, and the startup failures it is supposed to cause.

The point of parsing at startup is that a typo costs a crash-loop naming the
key, not a scale-up that quietly does nothing on the pass capacity was needed.
"""
import pathlib
import unittest

from support import make_config          # noqa: F401  (sys.path bootstrap)

from autoscaler.providers import cloudinit
from autoscaler.providers.base import ProviderConfigError
from autoscaler.providers.vultr.config import VultrConfig, VultrPlacement


class ConfigTest(unittest.TestCase):
    def test_defaults_need_no_keys_at_all(self):
        """Unlike OVH, which requires a project id, an account here is just a
        key — so an empty config block is legitimate."""
        cfg = VultrConfig.from_dict({})
        self.assertEqual(cfg.region, "ewr")
        self.assertEqual(cfg.api_url, "https://api.vultr.com/v2")

    def test_a_typo_is_rejected_rather_than_ignored(self):
        with self.assertRaises(ProviderConfigError) as e:
            VultrConfig.from_dict({"regionn": "ewr"})
        self.assertIn("regionn", str(e.exception))

    def test_os_id_must_be_a_number(self):
        with self.assertRaises(ProviderConfigError) as e:
            VultrConfig.from_dict({"osId": "ubuntu"})
        self.assertIn("osId", str(e.exception))

    def test_the_default_script_is_the_shared_one(self):
        """Not a copy under vultr/: two 226-line scripts drift, and the one that
        drifts is the one nobody is looking at."""
        cfg = VultrConfig.from_dict({})
        self.assertEqual(cfg.provision_script, cloudinit.DEFAULT_SCRIPT)
        self.assertTrue(pathlib.Path(cfg.provision_script).exists())

    def test_an_absolute_script_is_honoured_as_given(self):
        """The hot-patch escape hatch: mount a ConfigMap over it."""
        cfg = VultrConfig.from_dict({"provisionScript": "/config/provision.sh"})
        self.assertEqual(cfg.provision_script, "/config/provision.sh")

    def test_a_relative_script_resolves_against_providers(self):
        cfg = VultrConfig.from_dict({"provisionScript": "provision.sh"})
        self.assertEqual(cfg.provision_script, cloudinit.DEFAULT_SCRIPT)

    def test_a_trailing_slash_on_the_api_url_does_not_double_up(self):
        cfg = VultrConfig.from_dict({"apiUrl": "https://api.vultr.com/v2/"})
        self.assertEqual(cfg.api_url, "https://api.vultr.com/v2")


class PlacementTest(unittest.TestCase):
    def test_the_key_is_flavors_not_vultrs_own_word(self):
        """One name across every provider. Vultr calls these "plans" and OVH
        calls them "flavors"; making an operator remember which block they are
        filling in is a config-time trap for no gain, so the translation happens
        in provider.py where the API call is made."""
        p = VultrPlacement.from_dict({"flavors": ["vc2-2c-4gb"]})
        self.assertEqual(p.flavors, ("vc2-2c-4gb",))

    def test_vultrs_own_word_is_rejected_rather_than_ignored(self):
        """`plans` is what someone reading Vultr's docs will reach for. Silently
        accepting it would mean the list was never read and the pool rented the
        default."""
        with self.assertRaises(ProviderConfigError) as e:
            VultrPlacement.from_dict({"plans": ["vc2-2c-4gb"]})
        self.assertIn("plans", str(e.exception))

    def test_flavors_are_kept_in_the_order_given(self):
        """The list is a priority order, not a set: the first with stock wins."""
        p = VultrPlacement.from_dict({"flavors": ["vc2-4c-8gb", "vc2-2c-4gb"]})
        self.assertEqual(p.flavors, ("vc2-4c-8gb", "vc2-2c-4gb"))

    def test_a_bare_string_is_split_rather_than_iterated(self):
        """Iterating a string yields characters, so this would otherwise try to
        rent a flavor called "v"."""
        p = VultrPlacement.from_dict({"flavors": "vc2-2c-4gb, vc2-4c-8gb"})
        self.assertEqual(p.flavors, ("vc2-2c-4gb", "vc2-4c-8gb"))

    def test_no_flavors_is_a_startup_failure(self):
        with self.assertRaises(ProviderConfigError) as e:
            VultrPlacement.from_dict({"flavors": []})
        self.assertIn("nothing to rent", str(e.exception))

    def test_region_defaults_to_absent_so_the_account_region_wins(self):
        p = VultrPlacement.from_dict({"flavors": ["vc2-2c-4gb"]})
        self.assertIsNone(p.region)


if __name__ == "__main__":
    unittest.main()
