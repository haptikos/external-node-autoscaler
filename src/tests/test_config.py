"""The fleet file: providers, pools, and everything rejected at startup.

Rejection is the point. The chart passes both `config` blocks through verbatim
and can no longer tell you a key is wrong, so a typo that survived to runtime
would leave a default silently in force — a machine rented in the wrong region
on the wrong flavor, with nothing anywhere saying why.
"""
import os
import tempfile
import inspect
import unittest

from support import make_config          # noqa: F401  (sys.path bootstrap)

from autoscaler import config as config_mod

GOOD = """
providers:
  - name: ovh
    credentialsSecret: ovh-creds
    batchSize: 2
    config:
      project: abc123
pools:
  - name: gpu-a10
    nodeLabels:
      byon/pool: gpu-a10
    taints:
      - key: byon/pool
        value: gpu-a10
        effect: NoSchedule
    minMachines: 1
    maxMachines: 3
    providers:
      - name: ovh
        priority: 10
        flavors: [a10-45]
  - name: cpu-dryrun
    systemReserve: {cpu: 500m, memory: 1Gi}
    maxMachines: 4
    providers:
      - name: ovh
        flavors: [d2-8]
        gpu: false
"""


class FleetFileTest(unittest.TestCase):
    def load(self, text):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml",
                                         delete=False) as fh:
            fh.write(text)
            path = fh.name
        self.addCleanup(os.unlink, path)
        return config_mod._load_fleet_file(path)

    def fails(self, text, needle):
        """Every one of these is a SystemExit, not an exception that reaches the
        loop: a bad fleet file must be a crash-loop naming the key."""
        with self.assertRaises(SystemExit) as caught:
            self.load(text)
        self.assertIn(needle, str(caught.exception))

    # ------------------------------------------------------------- happy --
    def test_a_good_file_parses(self):
        providers, pools = self.load(GOOD)
        self.assertEqual([p.name for p in providers], ["ovh"])
        self.assertEqual(providers[0].batch_size, 2)
        self.assertEqual(providers[0].config, {"project": "abc123"})
        self.assertEqual([p.name for p in pools], ["gpu-a10", "cpu-dryrun"])

    def test_pool_order_is_preserved_because_order_is_priority(self):
        _, pools = self.load(GOOD)
        self.assertEqual(pools[0].name, "gpu-a10")

    def test_labels_and_taints_are_read(self):
        _, pools = self.load(GOOD)
        self.assertEqual(pools[0].node_labels, {"byon/pool": "gpu-a10"})
        self.assertEqual(pools[0].taints[0].effect, "NoSchedule")
        self.assertEqual(pools[1].taints, ())

    def test_reserves_default_when_a_pool_says_nothing(self):
        """Absent must mean the default, not zero: a pool that says nothing
        still has to leave room for k3s and Liqo."""
        _, pools = self.load(GOOD)
        self.assertFalse(pools[0].reserve().is_zero())

    def test_reserves_are_read_when_given(self):
        _, pools = self.load(GOOD)
        self.assertEqual(pools[1].system_reserve["cpu"], 500)
        self.assertEqual(pools[1].system_reserve["memory"], 1024 ** 3)

    def test_placement_keys_reach_the_provider_untouched(self):
        """Core config must not know what a flavor is; the provider validates
        it. Anything but name and priority is passed straight through."""
        _, pools = self.load(GOOD)
        self.assertEqual(pools[0].providers[0].config, {"flavors": ["a10-45"]})
        self.assertEqual(pools[1].providers[0].config,
                         {"flavors": ["d2-8"], "gpu": False})

    def test_a_bool_node_label_is_stringified(self):
        """YAML turns `gpu: true` into a bool; a node label must be a string, and
        the mismatch would surface as a selector that never matches."""
        _, pools = self.load(GOOD.replace(
            "      byon/pool: gpu-a10",
            "      byon/gpu: true"))
        self.assertEqual(pools[0].node_labels, {"byon/gpu": "true"})

    # -------------------------------------------------------- priorities --
    def test_pool_providers_are_sorted_by_priority(self):
        """Applied in the wrong order this rents from the expensive place first
        and nothing about the bill says why."""
        _, pools = self.load("""
providers:
  - {name: ovh, config: {project: p}}
  - {name: fake, config: {}}
pools:
  - name: p
    providers:
      - {name: ovh, priority: 50}
      - {name: fake, priority: 10}
""")
        self.assertEqual([e.name for e in pools[0].providers], ["fake", "ovh"])

    def test_equal_priorities_keep_declaration_order(self):
        _, pools = self.load("""
providers:
  - {name: ovh, config: {project: p}}
  - {name: fake, config: {}}
pools:
  - name: p
    providers:
      - {name: ovh}
      - {name: fake}
""")
        self.assertEqual([e.name for e in pools[0].providers], ["ovh", "fake"])

    # ------------------------------------------------------- rejections --
    def test_a_missing_file_is_fatal(self):
        with self.assertRaises(SystemExit) as caught:
            config_mod._load_fleet_file("/nonexistent/fleet.yaml")
        self.assertIn("no fleet config", str(caught.exception))

    def test_no_pools_is_fatal(self):
        self.fails("providers: [{name: ovh, config: {project: p}}]", "pools")

    def test_no_providers_is_fatal(self):
        self.fails("pools: [{name: p, providers: [{name: ovh}]}]", "providers")

    def test_a_duplicate_provider_type_is_rejected(self):
        """Credentials arrive as environment variables, so two OVH entries would
        both read OVH_APPLICATION_KEY and silently share one account."""
        self.fails("""
providers:
  - {name: ovh, config: {project: a}}
  - {name: ovh, config: {project: b}}
pools:
  - {name: p, providers: [{name: ovh}]}
""", "declared twice")

    def test_a_pool_naming_an_undeclared_provider_is_rejected(self):
        self.fails("""
providers: [{name: ovh, config: {project: p}}]
pools:
  - {name: p, providers: [{name: hetzner}]}
""", "hetzner")

    def test_scale_resource_is_gone_and_rejected(self):
        """It named a single dimension. Silently ignoring it would leave someone
        believing their pool still scales on GPUs alone."""
        self.fails("""
providers: [{name: ovh, config: {project: p}}]
pools:
  - {name: p, scaleResource: slots, providers: [{name: ovh}]}
""", "scaleResource")

    def test_an_unknown_pool_key_is_rejected(self):
        self.fails("""
providers: [{name: ovh, config: {project: p}}]
pools:
  - {name: p, maxMachine: 3, providers: [{name: ovh}]}
""", "maxMachine")

    def test_an_unknown_provider_key_is_rejected(self):
        self.fails("""
providers: [{name: ovh, credentialSecret: x, config: {project: p}}]
pools:
  - {name: p, providers: [{name: ovh}]}
""", "credentialSecret")

    def test_an_unknown_taint_key_is_rejected(self):
        self.fails("""
providers: [{name: ovh, config: {project: p}}]
pools:
  - name: p
    taints: [{key: k, values: v, effect: NoSchedule}]
    providers: [{name: ovh}]
""", "values")

    def test_a_bad_taint_effect_is_rejected(self):
        self.fails("""
providers: [{name: ovh, config: {project: p}}]
pools:
  - name: p
    taints: [{key: k, effect: NoSchedul}]
    providers: [{name: ovh}]
""", "NoSchedul")

    def test_the_default_batch_is_not_a_test_sized_number(self):
        """One number, three places. `batchSize` is a ceiling on how many
        machines one provider is asked for in a single pass -- a blast-radius
        limit, not a target, since maxMachines is what bounds the fleet. It sat
        at 3, which throttles a real scale-up into a dozen passes.

        Pinned because the constructors and the config default drifted apart
        once already: OVH said 100, Vultr said 3, ProviderSpec said 3 and the
        examples said 50, and only the ProviderSpec one had any effect.
        """
        from autoscaler.providers.base import DEFAULT_BATCH_SIZE
        from autoscaler.providers.ovh.provider import OvhProvider
        from autoscaler.providers.vultr.provider import VultrProvider

        self.assertGreaterEqual(DEFAULT_BATCH_SIZE, 50)
        self.assertEqual(config_mod.ProviderSpec(name="x").batch_size,
                         DEFAULT_BATCH_SIZE)
        for cls in (OvhProvider, VultrProvider):
            src = inspect.getsource(cls.__init__)
            self.assertIn("DEFAULT_BATCH_SIZE", src,
                          f"{cls.__name__} hardcodes its own batch size again")

    def test_a_batch_size_below_one_is_rejected(self):
        """A budget of zero is a loop with demand and headroom that rents
        nothing forever -- a stall with no error naming this key."""
        self.fails("""
providers: [{name: ovh, batchSize: 0, config: {project: p}}]
pools: [{name: p, providers: [{name: ovh}]}]
""", "batchSize")

    def test_min_above_max_is_rejected(self):
        self.fails("""
providers: [{name: ovh, config: {project: p}}]
pools:
  - {name: p, minMachines: 5, maxMachines: 2, providers: [{name: ovh}]}
""", "minMachines")

    def test_a_pool_with_no_providers_is_rejected(self):
        """It can never rent anything, so it is a pool in name only."""
        self.fails("""
providers: [{name: ovh, config: {project: p}}]
pools: [{name: p}]
""", "must be a non-empty list")

    def test_duplicate_pool_names_are_rejected(self):
        self.fails("""
providers: [{name: ovh, config: {project: p}}]
pools:
  - {name: p, providers: [{name: ovh}]}
  - {name: p, providers: [{name: ovh}]}
""", "duplicate")


if __name__ == "__main__":
    unittest.main()


class DrainTest(unittest.TestCase):
    """The teardown switch. A silent failure here leaves GPUs billing with
    nothing left to reap them, so it is tested rather than trusted."""

    def load(self, **env):
        import os
        with tempfile.NamedTemporaryFile("w", suffix=".yaml",
                                         delete=False) as fh:
            fh.write("""
providers: [{name: ovh, config: {project: p}}]
pools:
  - {name: warm, minMachines: 2, maxMachines: 5, providers: [{name: ovh}]}
""")
            path = fh.name
        self.addCleanup(os.unlink, path)
        keep = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(),
                                 os.environ.update(keep)))
        os.environ.update({"POD_NAMESPACE": "ns", "FLEET_CONFIG_FILE": path,
                           "MAX_MACHINES": "5", **env})
        return config_mod.Config.load()

    def test_without_drain_the_warm_floor_stands(self):
        cfg = self.load()
        self.assertEqual(cfg.pools[0].min_machines, 2)
        self.assertEqual(cfg.scale.max_machines, 5)

    def test_drain_overrides_per_pool_minimums(self):
        """minMachines is a floor rule 3 refuses to reap below, so a warm pool
        would otherwise hold its machines through the drain."""
        cfg = self.load(DRAIN="true")
        self.assertIs(cfg.drain, True)
        self.assertEqual(cfg.pools[0].min_machines, 0)

    def test_drain_does_not_express_itself_as_a_ceiling(self):
        """0 means UNLIMITED. A drain that zeroed maxMachines would invert into
        renting without bound, on the one path where that is unrecoverable."""
        cfg = self.load(DRAIN="true")
        self.assertEqual(cfg.pools[0].max_machines, 5, "ceiling was zeroed")

    def test_drain_defaults_off(self):
        self.assertIs(self.load().drain, False)
