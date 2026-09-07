"""Regression tests for the reconcile rules.

Every test here corresponds to something that actually went wrong on the live
system. They are cheap now only because the controller takes its collaborators
as arguments; before that, reproducing any of them meant renting a GPU.
"""
import unittest
import unittest.mock

from support import (DEFAULT_POOL, FakeClock, StubHub, StubLiqo, make_config,
                     make_pool, pod)

from autoscaler.config import ProviderSpec
from autoscaler.controller import Controller
from autoscaler.liqo import PeeringBlocked, TenantTerminating
from autoscaler.providers import flavor_shapes as provider_shapes
from autoscaler.hub import Hub
from autoscaler.providers.fake.provider import FakeProvider
from autoscaler.resources import Resources
from support import StubCore


def build(hub, provider=None, liqo=None, cfg=None, clock=None, names=None,
          providers=None):
    """A Controller wired to stubs.

    `provider` stays a single provider for the many tests that only care about
    the arithmetic; `providers` takes a {name: Provider} dict for the ones about
    fallback between them.
    """
    clock = clock or FakeClock()
    names = iter(names or [f"{i:04x}" for i in range(100)])
    # The hub shares the clock: credentials are aged by their Secret's
    # creationTimestamp, which the API server stamps, not by the controller.
    if hasattr(hub, "clock"):
        hub.clock = clock
    if providers is None:
        providers = {"fake": provider or FakeProvider()}
    cfg = cfg or make_config()
    # Same path __main__ uses, so tests exercise real shape resolution.
    shapes = provider_shapes(providers, cfg.pools)
    return Controller(hub, liqo or StubLiqo(), providers,
                      cfg, clock=clock, flavor_shapes=shapes,
                      namer=lambda: next(names)), clock


def fake(ctrl):
    """The one provider these fixtures wire up.

    Most tests here are about arithmetic, not about which cloud answered, so
    they build a controller with a single provider and reach through to it.
    """
    return next(iter(ctrl.providers.values()))


def ready_node(capacity=1, since=0.0):
    return {"ready": True, "since": since, "capacity": capacity}


class UsedOnCounting(unittest.TestCase):
    """What holds a machine alive.

    Two bugs live here. The first reaped the machine its own pod was waiting
    for: used_on() counted only Running pods, while pending_demand() ignores a
    bound pod because it is no longer Unschedulable, so both halves of the idle
    test read zero at once. The second is the mirror image -- a daemonset pod
    counting as work would hold every machine alive forever.
    """

    def used(self, pods):
        cfg = make_config(pools=[make_pool()])
        return Hub(StubCore(pods), None, cfg).used_on("external-ovh-0001")

    def test_bound_but_pending_pod_holds_capacity(self):
        self.assertFalse(self.used([pod(phase="Pending", gpus=1)]).is_zero())

    def test_running_pod_holds_capacity(self):
        self.assertFalse(self.used([pod(phase="Running", gpus=1)]).is_zero())

    def test_finished_pods_do_not(self):
        self.assertTrue(
            self.used([pod(phase="Succeeded"), pod(phase="Failed")]).is_zero())

    def test_terminating_pod_does_not(self):
        # A zombie Terminating pod on a virtual node — the failure the
        # pod-reaper exists for — would otherwise pin a rented machine alive.
        self.assertTrue(self.used([pod(phase="Running", terminating=True)]).is_zero())

    def test_a_daemonset_pod_does_not_hold_the_machine(self):
        """liqo-pod-reaper fences DaemonSets off virtual nodes, but only the
        ones it is told about and only every five minutes. One that slips
        through would hold used_on above zero forever, the idle reaper would
        never fire, and the machine would bill until somebody noticed."""
        self.assertTrue(self.used([pod(daemonset=True, gpus=0,
                                       requests={"cpu": "100m"})]).is_zero())

    def test_an_ordinary_pod_alongside_a_daemonset_still_does(self):
        self.assertFalse(self.used([
            pod(daemonset=True, gpus=0, requests={"cpu": "100m"}),
            pod(gpus=1)]).is_zero())

    def test_it_sums_every_dimension(self):
        used = self.used([pod(gpus=0, requests={"cpu": "1", "memory": "2Gi"}),
                          pod(gpus=0, requests={"cpu": "500m"})])
        self.assertEqual(used["cpu"], 1500)
        self.assertEqual(used["memory"], 2 * 1024 ** 3)
        self.assertEqual(used["pods"], 2)


class IdleReaping(unittest.TestCase):
    def test_busy_node_is_never_reaped(self):
        hub = StubHub(demand=0, vnodes={"external-ovh-0001": ready_node()},
                      used={"external-ovh-0001": 1})
        liqo = StubLiqo(peered={"external-ovh-0001"})
        ctrl, clock = build(hub, liqo=liqo)
        fake(ctrl).machines["external-ovh-0001"] = _machine("external-ovh-0001", clock())

        for _ in range(3):
            ctrl.reconcile()
            clock.advance(10_000)          # far past idle_s
        self.assertEqual(fake(ctrl).destroyed, [])

    def test_idle_node_is_reaped_only_after_idle_seconds(self):
        hub = StubHub(demand=0, vnodes={"external-ovh-0001": ready_node()},
                      used={"external-ovh-0001": 0},
                      credentials=creds("external-ovh-0001"))
        liqo = StubLiqo(peered={"external-ovh-0001"}, sliced={"external-ovh-0001"})
        ctrl, clock = build(hub, liqo=liqo, cfg=make_config(scale={"idle_s": 600}))
        fake(ctrl).machines["external-ovh-0001"] = _machine("external-ovh-0001", clock())

        ctrl.reconcile()                    # marks idle_since
        self.assertEqual(fake(ctrl).destroyed, [])
        clock.advance(300)
        ctrl.reconcile()
        self.assertEqual(fake(ctrl).destroyed, [], "reaped before idle_s")
        clock.advance(400)                  # now past 600s
        ctrl.reconcile()
        self.assertEqual(fake(ctrl).destroyed, ["external-ovh-0001"])
        self.assertEqual(liqo.unpeered, ["external-ovh-0001"], "should unpeer gracefully")


class OrphanConfirmation(unittest.TestCase):
    """The bug that killed a serving GPU box.

    The provider inventory came back without a healthy machine for one pass.
    Rule 6 acted on that single observation, tore down the peering, and the vLLM
    pod was evicted — losing a 15GB model cache and the price of a re-rent.
    """

    def setUp(self):
        self.hub = StubHub()
        self.liqo = StubLiqo(peered={"external-ovh-0001"})
        self.cfg = make_config(scale={"missing_confirm_s": 90})
        self.ctrl, self.clock = build(self.hub, liqo=self.liqo, cfg=self.cfg)
        # Peered, but the provider does not list it — the exact situation.

    def test_single_missing_observation_does_not_tear_down(self):
        self.ctrl.reconcile()
        self.assertEqual(self.liqo.torn_down, [])

    def test_teardown_only_after_the_confirmation_window(self):
        self.ctrl.reconcile()
        self.clock.advance(60)
        self.ctrl.reconcile()
        self.assertEqual(self.liqo.torn_down, [], "tore down inside the window")
        self.clock.advance(60)              # 120s total, past 90s
        self.ctrl.reconcile()
        self.assertEqual(self.liqo.torn_down, ["external-ovh-0001"])

    def test_machine_reappearing_clears_the_mark(self):
        self.ctrl.reconcile()
        self.clock.advance(60)
        fake(self.ctrl).machines["external-ovh-0001"] = _machine(
            "external-ovh-0001", self.clock())
        self.ctrl.reconcile()
        self.assertNotIn("external-ovh-0001", self.ctrl.missing_since)

        # And the window restarts from scratch if it goes missing again, rather
        # than resuming where it left off.
        del fake(self.ctrl).machines["external-ovh-0001"]
        self.clock.advance(60)
        self.ctrl.reconcile()
        self.assertEqual(self.liqo.torn_down, [])


class Oversubscription(unittest.TestCase):
    def test_negative_headroom_clamps_to_zero_and_does_not_hide_real_capacity(self):
        # One node with 2 pods on a 1-unit slice, one node with 1 unit free.
        # Unclamped, free would be (1-3) + 1 = -1 and the loop would rent a
        # machine it does not need.
        hub = StubHub(demand=1,
                      vnodes={"external-ovh-0001": ready_node(capacity=1),
                              "external-ovh-0002": ready_node(capacity=1)},
                      used={"external-ovh-0001": 3, "external-ovh-0002": 0})
        ctrl, clock = build(hub)
        for n in ("external-ovh-0001", "external-ovh-0002"):
            fake(ctrl).machines[n] = _machine(n, clock())
        ctrl.reconcile()
        self.assertEqual(fake(ctrl).created, [],
                         "clamping failed: spurious scale-up")


class ScaleUp(unittest.TestCase):
    def test_demand_is_packed_into_whole_machines(self):
        """Four one-CPU pods against machines holding two of them: two machines,
        not four. What unitsPerMachine used to assert, now derived from the
        machine's actual shape."""
        hub = StubHub(demand=4)
        ctrl, _ = build(hub,
                        provider=FakeProvider(capacity={
                            "fake-pair": Resources({"cpu": "2", "pods": 2})}),
                        cfg=make_config(scale={"max_machines": 10}))
        ctrl.reconcile()
        self.assertEqual(len(fake(ctrl).created), 2)

    def test_max_machines_is_a_hard_ceiling(self):
        hub = StubHub(demand=99)
        ctrl, _ = build(hub, cfg=make_config(scale={"max_machines": 3}))
        ctrl.reconcile()
        self.assertEqual(len(fake(ctrl).created), 3)

    def test_batch_size_caps_creations_per_pass(self):
        """Each create() is a blocking API call, so an unbounded burst spends a
        whole interval inside scale-up."""
        hub = StubHub(demand=9)
        ctrl, _ = build(hub, cfg=make_config(
            scale={"max_machines": 99}, pools=[make_pool(max_machines=99)],
            providers=[ProviderSpec(name="fake", batch_size=3)]))
        ctrl.reconcile()
        self.assertEqual(len(fake(ctrl).created), 3)

    def test_the_remainder_is_rented_on_later_passes_not_dropped(self):
        """A per-pass cap, not a truncation: nothing tracks the remainder
        because the next pass recomputes the deficit."""
        hub = StubHub(demand=9)
        ctrl, clock = build(hub, cfg=make_config(
            scale={"max_machines": 99}, pools=[make_pool(max_machines=99)],
            providers=[ProviderSpec(name="fake", batch_size=3)]))
        for _ in range(3):
            ctrl.reconcile()
            clock.advance(30)
        self.assertEqual(len(fake(ctrl).created), 9)

    def test_max_machines_still_wins_over_a_larger_batch(self):
        hub = StubHub(demand=99)
        ctrl, _ = build(hub, provider=FakeProvider(batch_size=10),
                        cfg=make_config(scale={"max_machines": 2}))
        ctrl.reconcile()
        self.assertEqual(len(fake(ctrl).created), 2)

    def test_a_small_deficit_is_not_padded_up_to_the_batch(self):
        """A ceiling, not a target."""
        hub = StubHub(demand=1)
        ctrl, _ = build(hub, provider=FakeProvider(batch_size=3),
                        cfg=make_config(scale={"max_machines": 99}))
        ctrl.reconcile()
        self.assertEqual(len(fake(ctrl).created), 1)

    def test_reaching_the_ceiling_with_standing_demand_says_so(self):
        """Otherwise "capped as configured" and "wedged" look identical."""
        hub = StubHub(demand=99)
        ctrl, _ = build(hub, cfg=make_config(scale={"max_machines": 1}))
        ctrl.reconcile()                     # rents its one machine
        # No logging.disable() dance: support.py silences by raising the ROOT
        # level, which assertLogs overrides on the named logger. Loggers are
        # named by failure domain, not by module path — see logging_setup.get.
        with self.assertLogs("controller", level="INFO") as caught:
            ctrl.reconcile()
        self.assertTrue(
            any("maxMachines=1 is reached" in m for m in caught.output),
            f"no ceiling explanation in: {caught.output}")
        self.assertEqual(len(fake(ctrl).created), 1, "rented past the ceiling")

    def test_names_carry_the_prefix_and_the_provider(self):
        # external-<provider>-<id>, so a box in the OVH console says what made it.
        ctrl, _ = build(StubHub(demand=1))
        ctrl.reconcile()
        self.assertRegex(fake(ctrl).created[0], r"^external-fake-[0-9a-f]+$")

    def test_matching_ignores_the_provider_segment(self):
        # A machine rented by a DIFFERENT provider must still be visible, or
        # switching provider would strand the previous one's machines: no reap
        # path would see them and they would bill forever.
        ctrl, clock = build(StubHub())
        fake(ctrl).machines["external-ovh-0001"] = _machine(
            "external-ovh-0001", clock())
        self.assertIn("external-ovh-0001", ctrl.machines())

    def test_out_of_stock_stops_after_one_attempt(self):
        # Returning False means "no capacity", not "error". Asking four more
        # times in the same pass gets the same answer and four more log lines.
        provider = FakeProvider(out_of_stock=True)
        ctrl, _ = build(StubHub(demand=5), provider=provider,
                        cfg=make_config(scale={"max_machines": 5}))
        ctrl.reconcile()
        self.assertEqual(provider.created, [])
        self.assertEqual(provider.machines, {})

    def test_booting_machines_count_as_supply(self):
        # Otherwise every pass while a machine boots would rent another one.
        hub = StubHub(demand=1)
        ctrl, clock = build(hub)
        ctrl.reconcile()
        self.assertEqual(len(fake(ctrl).created), 1)
        clock.advance(60)
        ctrl.reconcile()
        self.assertEqual(len(fake(ctrl).created), 1, "double-provisioned")


class OwnershipFilter(unittest.TestCase):
    def test_machines_outside_the_prefix_are_ignored(self):
        # The hand-managed A10 cluster must be unreachable from every code path
        # here. A provider bug returning it is not allowed to be fatal.
        ctrl, clock = build(StubHub())
        fake(ctrl).machines["someone-elses-box"] = _machine(
            "someone-elses-box", clock())
        ctrl.reconcile()
        self.assertEqual(fake(ctrl).destroyed, [])


class BringUp(unittest.TestCase):
    def test_peering_and_slicing_are_independent_steps(self):
        # A slice failure must not leave the machine marked peered with no
        # capacity and nothing retrying it.
        hub = StubHub(credentials=creds("external-ovh-0001"))
        liqo = StubLiqo(peered={"external-ovh-0001"})     # peered, not sliced
        ctrl, clock = build(hub, liqo=liqo)
        fake(ctrl).machines["external-ovh-0001"] = _machine("external-ovh-0001", clock())
        ctrl.reconcile()
        self.assertEqual(liqo.peered_calls, [], "re-peered an already-peered box")
        self.assertEqual(liqo.sliced_calls, ["external-ovh-0001"])

    def test_bring_up_failure_does_not_abort_the_pass(self):
        hub = StubHub(demand=1, credentials=creds("external-ovh-0001"))
        liqo = StubLiqo()
        liqo.peer = _raise("peering exploded")
        ctrl, clock = build(hub, liqo=liqo)
        fake(ctrl).machines["external-ovh-0001"] = _machine("external-ovh-0001", clock())
        ctrl.reconcile()        # must not raise; later rules still run


class Teardown(unittest.TestCase):
    def test_failed_graceful_unpeer_falls_back_to_force(self):
        hub = StubHub(demand=0, vnodes={"external-ovh-0001": ready_node()},
                      used={"external-ovh-0001": 0},
                      credentials=creds("external-ovh-0001"))
        liqo = StubLiqo(peered={"external-ovh-0001"}, sliced={"external-ovh-0001"})
        liqo.unpeer_raises = True
        ctrl, clock = build(hub, liqo=liqo, cfg=make_config(scale={"idle_s": 0}))
        fake(ctrl).machines["external-ovh-0001"] = _machine("external-ovh-0001", clock())
        ctrl.reconcile()
        clock.advance(10)
        ctrl.reconcile()
        self.assertEqual(liqo.torn_down, ["external-ovh-0001"])
        self.assertEqual(fake(ctrl).destroyed, ["external-ovh-0001"])
        self.assertEqual(hub.dropped, ["external-ovh-0001"])

    def test_residue_after_a_successful_unpeer_is_swept_before_the_box_dies(self):
        """Observed in production: unpeer reports success, the ForeignCluster is
        still there, and 36s later the machine is destroyed. Liqo's crdreplicator
        finalizers clear by talking to the REMOTE cluster, so destroying it first
        strands them — leaving a Terminating tenant namespace whose live
        NamespaceMap disables offloading for every cluster."""
        hub = StubHub(demand=0, vnodes={"external-ovh-0001": ready_node()},
                      used={"external-ovh-0001": 0},
                      credentials=creds("external-ovh-0001"))
        liqo = StubLiqo(peered={"external-ovh-0001"}, sliced={"external-ovh-0001"})
        liqo.unpeer_leaves_residue = True
        ctrl, clock = build(hub, liqo=liqo, cfg=make_config(scale={"idle_s": 0}))
        fake(ctrl).machines["external-ovh-0001"] = _machine(
            "external-ovh-0001", clock())
        ctrl.reconcile()
        clock.advance(10)
        ctrl.reconcile()

        self.assertEqual(liqo.unpeered, ["external-ovh-0001"])
        self.assertEqual(liqo.swept, ["external-ovh-0001"],
                         "residue left for rule 6 to find 90s later")
        self.assertEqual(fake(ctrl).destroyed, ["external-ovh-0001"])

    def test_a_clean_unpeer_is_still_swept(self):
        """Unconditional. `liqoctl unpeer` is not asked to delete the tenant
        namespace (it waits on finalizers it cannot clear and burns the whole
        peer_timeout doing it), so the sweep is what removes it -- including
        when unpeer reported no residue at all. force_teardown tolerates 404
        everywhere, so the cost of being wrong is a few no-op deletes; the cost
        of skipping it is a namespace stuck Terminating, which keeps its name
        taken and blocks any future machine that reuses it."""
        hub = StubHub(demand=0, vnodes={"external-ovh-0001": ready_node()},
                      used={"external-ovh-0001": 0},
                      credentials=creds("external-ovh-0001"))
        liqo = StubLiqo(peered={"external-ovh-0001"}, sliced={"external-ovh-0001"})
        ctrl, clock = build(hub, liqo=liqo, cfg=make_config(scale={"idle_s": 0}))
        fake(ctrl).machines["external-ovh-0001"] = _machine(
            "external-ovh-0001", clock())
        ctrl.reconcile()
        clock.advance(10)
        ctrl.reconcile()
        self.assertEqual(liqo.unpeered, ["external-ovh-0001"])
        self.assertEqual(liqo.swept, ["external-ovh-0001"],
                         "the sweep is how the tenant namespace goes away")

    def test_condemned_machine_is_reaped_regardless_of_timers(self):
        hub = StubHub(condemned=["external-ovh-0001"])
        liqo = StubLiqo(peered={"external-ovh-0001"})
        ctrl, clock = build(hub, liqo=liqo)
        fake(ctrl).machines["external-ovh-0001"] = _machine("external-ovh-0001", clock())
        ctrl.reconcile()
        self.assertEqual(fake(ctrl).destroyed, ["external-ovh-0001"])
        self.assertEqual(hub.unmarked, ["external-ovh-0001"])

    def test_condemning_beats_a_timer_that_would_also_reap_it(self):
        """The ONE rule ordering that is load-bearing.

        recycle_condemned pops what it reaps out of the snapshot, so the timer
        rules skip it. Run it after them and a condemned machine that is also
        past its boot timeout is reaped by whichever timer gets there first --
        destroyed twice, and the log blames a timeout for what an operator
        asked for.
        """
        hub = StubHub(condemned=["external-ovh-0001"])
        ctrl, clock = build(hub, cfg=make_config(scale={"boot_s": 1200}))
        # Old enough that the boot timeout would claim it too.
        fake(ctrl).machines["external-ovh-0001"] = _machine(
            "external-ovh-0001", clock() - 1300)
        with self.assertLogs("controller", level="INFO") as caught:
            ctrl.reconcile()
        out = "\n".join(caught.output)
        self.assertEqual(fake(ctrl).destroyed, ["external-ovh-0001"],
                         "reaped more than once, or not at all")
        self.assertIn("condemned by operator", out)
        self.assertNotIn("boot timeout", out)


    def test_boot_timeout_reaps_a_machine_that_never_joined(self):
        hub = StubHub()
        ctrl, clock = build(hub, cfg=make_config(scale={"boot_s": 1200}))
        fake(ctrl).machines["external-ovh-0001"] = _machine("external-ovh-0001", clock())
        ctrl.reconcile()
        self.assertEqual(fake(ctrl).destroyed, [], "reaped while still booting")
        clock.advance(1300)
        ctrl.reconcile()
        self.assertEqual(fake(ctrl).destroyed, ["external-ovh-0001"])

    def test_drain_stops_renting_but_not_reaping(self):
        """scale_up returns early under drain. That early return is only safe
        because it is its own method: in the old single reconcile() the same
        `return` would have skipped every teardown rule below it -- during a
        drain, which is exactly `make down` releasing machines before the hub
        is destroyed. Nothing left alive to reap them, and they bill on.
        """
        hub = StubHub(demand=3)
        ctrl, clock = build(hub, cfg=make_config(drain=True,
                                                 scale={"boot_s": 1200}))
        fake(ctrl).machines["external-ovh-0001"] = _machine(
            "external-ovh-0001", clock() - 1300)     # past its boot timeout
        ctrl.reconcile()
        self.assertEqual(fake(ctrl).created, [], "rented during a drain")
        self.assertEqual(fake(ctrl).destroyed, ["external-ovh-0001"],
                         "drain skipped teardown -- the machine bills forever")

    def test_dead_node_reaped_after_dead_seconds(self):
        hub = StubHub(vnodes={"external-ovh-0001": {"ready": False, "since": 0.0,
                                             "capacity": 1}})
        ctrl, clock = build(hub, cfg=make_config(scale={"dead_s": 900}))
        hub.vnodes["external-ovh-0001"]["since"] = clock()
        fake(ctrl).machines["external-ovh-0001"] = _machine("external-ovh-0001", clock())
        ctrl.reconcile()
        self.assertEqual(fake(ctrl).destroyed, [])
        clock.advance(1000)
        ctrl.reconcile()
        self.assertEqual(fake(ctrl).destroyed, ["external-ovh-0001"])


class PreSeededCredentials(unittest.TestCase):
    """The bootstrap the machine no longer takes part in.

    The operator mints each machine's certificate authorities before renting it,
    so the Secret it used to wait for is one it now writes itself. Everything
    below is a consequence of that inversion, and each case is one the old
    design made structurally impossible.
    """

    def test_credentials_are_persisted_before_the_machine_is_created(self):
        """The crash window: credentials exist only in memory until the write
        lands, and a live box holding CAs whose counterpart is gone is
        unreachable, unpeerable, and billing. Asserted against provision()
        because reconcile deliberately swallows this now."""
        hub = StubHub(demand=1)
        ctrl, _ = build(hub, provider=FakeProvider(create_raises=True))
        pool = ctrl.cfg.pools[0]
        with self.assertRaises(RuntimeError):
            ctrl.provision(pool, pool.providers[0])
        self.assertEqual(len(hub.credentials), 1,
                         "create() was reached before the credentials were saved")

    def test_a_provider_exception_does_not_abort_the_pass(self):
        """Found in production on an OVH quota error: a raising provider
        propagated out of reconcile(), skipping every rule below scale-up. The
        one fault that stops the operator RENTING machines also stopped it
        RELEASING them."""
        hub = StubHub(demand=1)
        ctrl, clock = build(hub, provider=FakeProvider(),
                            cfg=make_config(scale={"boot_s": 1200}))
        # A machine that will hit its boot timeout, i.e. work for rule 5.
        fake(ctrl).machines["external-ovh-0001"] = _machine(
            "external-ovh-0001", clock())
        clock.advance(1300)
        fake(ctrl).create_raises = True

        ctrl.reconcile()                       # must not raise
        self.assertEqual(fake(ctrl).destroyed, ["external-ovh-0001"],
                         "a failed scale-up stopped the loop reaping")

    def test_an_exception_leaves_the_credentials_for_rule_6_to_confirm(self):
        """A timeout after the provider accepted the request is
        indistinguishable from one that never arrived, so hold rather than
        drop."""
        hub = StubHub(demand=1)
        ctrl, _ = build(hub, provider=FakeProvider(create_raises=True))
        ctrl.reconcile()
        self.assertEqual(hub.dropped, [])
        self.assertEqual(len(hub.credentials), 1)

    def test_a_definite_no_capacity_drops_the_credentials_at_once(self):
        """A definite "nothing was created", so the credentials go at once
        rather than lingering for boot_s."""
        hub = StubHub(demand=1)
        ctrl, _ = build(hub, provider=FakeProvider(out_of_stock=True))
        ctrl.reconcile()
        self.assertEqual(len(hub.dropped), 1)
        self.assertEqual(hub.credentials, {})

    def test_unready_machine_is_not_peered(self):
        """Peering mid-boot blocks the loop for the full liqoctl timeout, and
        before the device plugin lands it advertises a zero-GPU slice."""
        hub = StubHub(credentials=creds("external-ovh-0001"))
        liqo = StubLiqo(ready=set())            # boot script still running
        ctrl, clock = build(hub, liqo=liqo)
        fake(ctrl).machines["external-ovh-0001"] = _machine(
            "external-ovh-0001", clock())
        ctrl.reconcile()
        self.assertEqual(liqo.peered_calls, [])
        self.assertEqual(liqo.ready_calls, ["external-ovh-0001"])

    def test_machine_is_peered_once_it_reports_ready(self):
        hub = StubHub(credentials=creds("external-ovh-0001"))
        liqo = StubLiqo(ready=set())
        ctrl, clock = build(hub, liqo=liqo)
        fake(ctrl).machines["external-ovh-0001"] = _machine(
            "external-ovh-0001", clock())
        ctrl.reconcile()
        liqo._ready = {"external-ovh-0001"}     # last line of provision.sh ran
        ctrl.reconcile()
        self.assertEqual(liqo.peered_calls, ["external-ovh-0001"])

    def test_readiness_is_not_rechecked_once_peered(self):
        """Self-limiting, or every pass pays a round trip per machine."""
        hub = StubHub(credentials=creds("external-ovh-0001"))
        liqo = StubLiqo(peered={"external-ovh-0001"},
                        sliced={"external-ovh-0001"})
        ctrl, clock = build(hub, liqo=liqo)
        fake(ctrl).machines["external-ovh-0001"] = _machine(
            "external-ovh-0001", clock())
        ctrl.reconcile()
        self.assertEqual(liqo.ready_calls, [])

    def test_a_machine_peered_this_pass_is_labelled_in_the_same_pass(self):
        """A virtual node with no pool labels matches no pod's nodeSelector, so
        every pass it goes unlabelled is a pass the pods it was rented for stay
        Pending -- and keep asking for more machines.

        `peered` used to be a snapshot taken before the bring-up loop, and the
        labelling sweep intersected with it, so a machine peered during the pass
        was invisible until the next one. With peering serial at ~30s a machine,
        that is minutes; a machine that stops answering mid-handshake stretches
        it to the full liqoctl timeout. Live, ten machines came up and exactly
        one of them -- the one already peered when the pass began -- carried its
        pool's labels and taints.
        """
        c = creds("external-ovh-0001")
        c["external-ovh-0001"].pool = "default"
        hub = StubHub(credentials=c)
        liqo = StubLiqo()                       # nothing peered yet
        ctrl, clock = build(hub, liqo=liqo)
        fake(ctrl).machines["external-ovh-0001"] = _machine(
            "external-ovh-0001", clock())
        ctrl.reconcile()
        self.assertEqual(liqo.peered_calls, ["external-ovh-0001"])
        self.assertEqual(liqo.labelled, [("external-ovh-0001", "default")],
                         "peered this pass but not labelled until the next one")

    def test_a_node_is_labelled_while_later_machines_are_still_peering(self):
        """Labels land per machine, not once the whole pass is done.

        Peering is serial at ~25-30s a machine, and one handshake in a live run
        took 3m18s on its own. A node labelled only at the end of the loop is
        rented, joined and billing for all of that while matching no pod's
        nodeSelector -- so the pods it was rented for stay Pending and the pool
        keeps asking for more machines.

        vnode_delay=1 models the real asynchrony: Liqo creates the VirtualNode
        seconds AFTER the slice, so the machine cannot settle on the ask that
        immediately follows its own peering -- only on a later one, which is
        exactly the case a per-machine sweep has to handle.
        """
        names = [f"external-ovh-000{i}" for i in (1, 2, 3)]
        c = creds(*names)
        for cred in c.values():
            cred.pool = "default"
        hub = StubHub(credentials=c)
        liqo = StubLiqo(vnode_delay=1)
        ctrl, clock = build(hub, liqo=liqo)
        for n in names:
            fake(ctrl).machines[n] = _machine(n, clock())
        ctrl.reconcile()

        self.assertEqual(liqo.peered_calls, names)
        # The LAST ask about it, since vnode_delay=1 means the first one found
        # no VirtualNode and settled nothing.
        settled = len(liqo.events) - 1 - liqo.events[::-1].index(
            ("label", names[0]))
        last_peer = liqo.events.index(("peer", names[-1]))
        self.assertLess(settled, last_peer,
                        "the first machine was still unlabelled when the last "
                        "one started its handshake")

    def test_labelling_is_not_starved_by_a_slow_peering(self):
        """The sweep runs BEFORE the bring-up loop as well as after it.

        Peering is one liqoctl handshake at a time and a machine that stops
        answering holds the loop for liqo.peer_timeout. Machines that are
        already up must not wait behind that to become schedulable.
        """
        c = creds("external-ovh-0001", "external-ovh-0002")
        for cred in c.values():
            cred.pool = "default"
        hub = StubHub(credentials=c)
        liqo = StubLiqo(peered={"external-ovh-0001"},
                        sliced={"external-ovh-0001"})
        ctrl, clock = build(hub, liqo=liqo)
        for n in ("external-ovh-0001", "external-ovh-0002"):
            fake(ctrl).machines[n] = _machine(n, clock())
        ctrl.reconcile()
        first_label = liqo.events.index(("label", "external-ovh-0001"))
        peer_0002 = liqo.events.index(("peer", "external-ovh-0002"))
        self.assertLess(first_label, peer_0002,
                        "an already-peered node waited behind a fresh handshake "
                        "to be labelled")

    def test_machine_without_an_address_is_skipped_not_failed(self):
        """The window before an address exists must produce no kubeconfig, no
        peering and no exception."""
        hub = StubHub(demand=1)
        ctrl, clock = build(hub, provider=FakeProvider(assign_ip=False))
        ctrl.reconcile()                        # rents one
        liqo = ctrl.liqo
        ctrl.reconcile()                        # sees it, cannot reach it
        self.assertEqual(liqo.peered_calls, [])
        self.assertEqual(liqo.ready_calls, [],
                         "probed a machine with no address")

    def test_credentials_survive_the_window_before_the_machine_is_listed(self):
        """Deleting inside the create-to-inventory window destroys the only
        copy of credentials for a machine that may be booting right now."""
        hub = StubHub(demand=1)
        # create() succeeds, but the instance does not appear in the inventory —
        # the real gap this guards, and the one where deleting is unrecoverable
        # because the CA private keys exist only on that machine and here.
        ctrl, clock = build(hub, provider=FakeProvider(hide_created=True),
                            cfg=make_config(scale={"boot_s": 1200}))
        ctrl.reconcile()
        self.assertEqual(len(hub.credentials), 1)
        hub.demand = 0                          # stop renting, watch the one machine

        # The window opens when the operator first OBSERVES credentials with no
        # machine, which is the pass after they were written — credentials are
        # read at the top of a pass and provision() runs later in it. Later than
        # strictly necessary, and deliberately so: every rounding here is
        # towards waiting longer.
        clock.advance(600)
        ctrl.reconcile()
        self.assertEqual(hub.dropped, [])
        clock.advance(600)                      # 600s into the window
        ctrl.reconcile()
        self.assertEqual(hub.dropped, [], "deleted credentials inside boot_s")

        # And if the machine turns up before the window closes, the credentials
        # were never at risk.
        name = fake(ctrl).created[0]
        fake(ctrl).hidden.clear()
        ctrl.reconcile()
        self.assertEqual(hub.dropped, [])
        self.assertIn(name, hub.credentials)

    def test_orphan_age_survives_an_operator_restart(self):
        """The bug that put 91 Secrets in the namespace: the age was tracked
        in a dict on the Controller, so every restart reset it and through a
        spell of redeploys nothing ever reached boot_s. The Secret's own
        creationTimestamp is the only clock a restart cannot rewind."""
        hub = StubHub(demand=1)
        cfg = make_config(scale={"boot_s": 1200})
        ctrl, clock = build(hub, provider=FakeProvider(hide_created=True),
                            cfg=cfg)
        ctrl.reconcile()
        name = fake(ctrl).created[0]
        hub.demand = 0

        # Restart the operator repeatedly, well past boot_s in total. A fresh
        # Controller each time is exactly what a redeploy does.
        for _ in range(5):
            clock.advance(400)
            ctrl, _ = build(hub, provider=fake(ctrl), cfg=cfg, clock=clock)
            ctrl.reconcile()

        self.assertIn(name, hub.dropped,
                      "orphan outlived boot_s because restarts reset its age")

    def test_never_peered_machine_is_torn_down_without_a_graceful_unpeer(self):
        """`liqoctl unpeer` against a machine that never peered spends the whole
        peer timeout discovering there is nothing to undo, and a kubeconfig now
        exists for such machines. Modelled as a virtual node that outlived its
        peering."""
        hub = StubHub(demand=0, vnodes={"external-ovh-0001": ready_node()},
                      used={"external-ovh-0001": 0},
                      credentials=creds("external-ovh-0001"))
        liqo = StubLiqo(ready=set())            # never becomes peered
        ctrl, clock = build(hub, liqo=liqo, cfg=make_config(scale={"idle_s": 0}))
        fake(ctrl).machines["external-ovh-0001"] = _machine(
            "external-ovh-0001", clock())
        ctrl.reconcile()
        clock.advance(10)
        ctrl.reconcile()
        self.assertEqual(liqo.unpeered, [])
        self.assertEqual(liqo.torn_down, ["external-ovh-0001"])

    def test_endpoint_is_recorded_for_a_machine_we_reached(self):
        hub = StubHub(credentials=creds("external-ovh-0001"))
        ctrl, clock = build(hub)
        fake(ctrl).machines["external-ovh-0001"] = _machine(
            "external-ovh-0001", clock(), ip="203.0.113.5")
        ctrl.reconcile()
        self.assertEqual(hub.endpoints["external-ovh-0001"],
                         "https://203.0.113.5:6443")


class ProviderIgnoringTheNamePrefix(unittest.TestCase):
    """list_machines() is GIVEN the prefix, and both real providers filter on
    it, so anything discarded afterwards means the provider ignored it."""

    def build_returning(self, names):
        provider = FakeProvider()
        provider.list_machines = lambda name_prefix="": {
            n: _machine(n, 1_000_000.0) for n in names}
        return build(StubHub(), provider=provider)[0]

    def test_discarding_every_machine_is_a_warning(self):
        """Downstream this is indistinguishable from "the cloud has no
        machines" -- which is how a whole fleet goes unreapable and bills on."""
        ctrl = self.build_returning(["someone-elses-box", "another-one"])
        with self.assertLogs("controller", level="WARNING") as caught:
            ctrl.reconcile()
        out = "\n".join(caught.output)
        self.assertIn("NONE match", out)
        self.assertIn("external-", out)

    def test_discarding_some_is_only_debug(self):
        """A shared account legitimately holds machines that are not ours."""
        ctrl = self.build_returning(["external-ovh-0001", "someone-elses-box"])
        with self.assertRaises(AssertionError):
            with self.assertLogs("controller", level="WARNING"):
                ctrl.reconcile()

    def test_an_empty_provider_is_silent(self):
        """A wrong name_prefix cannot be detected here: the provider filters
        first and simply returns nothing. Warning on empty would fire on every
        healthy idle cloud."""
        ctrl = self.build_returning([])
        with self.assertRaises(AssertionError):
            with self.assertLogs("controller", level="WARNING"):
                ctrl.reconcile()


class PassLoggingIsNotRepetitive(unittest.TestCase):
    """Flavor shapes are fixed for the life of the process. Printing them every
    ~12s pushed the four numbers that DO change off the right of the line."""

    def build(self, **cfg_over):
        return build(StubHub(), cfg=make_config(**cfg_over))[0]

    def test_the_pass_line_does_not_repeat_the_shapes(self):
        ctrl = self.build()
        with self.assertLogs("controller", level="INFO") as caught:
            ctrl.reconcile()
        out = "\n".join(caught.output)
        self.assertIn("pending=", out)
        self.assertNotIn("Resources(", out)

    def test_the_shapes_are_still_reported_once(self):
        ctrl = self.build()
        with self.assertLogs("controller", level="INFO") as caught:
            ctrl.log_flavors()
        self.assertIn("flavors:", "\n".join(caught.output))

    def test_a_pool_with_no_known_shapes_says_so_every_pass(self):
        """That state also silences scale_up's never-fits error, so it must not
        be visible only in a startup line that has scrolled away."""
        ctrl = self.build()
        ctrl._flavor_shapes = {}                 # nothing could be resolved
        with self.assertLogs("controller", level="INFO") as caught:
            ctrl.reconcile()
        self.assertIn("flavors=unknown", "\n".join(caught.output))


class RefusedPeeringBacksOff(unittest.TestCase):
    """Abandoning a doomed handshake early is only half the job: the next pass
    started the identical handshake again, trading one long stall for a shorter
    one repeated forever, in front of every machine queued behind it."""

    #: The refused machine's gateway pod, still Pending on the hub. Nothing
    #: deletes it when the handshake is abandoned -- it outlives the attempt,
    #: and the live log shows it ageing across passes -- so its presence is the
    #: standing answer to "is the hub still refusing this machine".
    def refused_pod(self, age_s=30):
        return {"name": "gw-external-ovh-0001",
                "machine": "external-ovh-0001",
                "namespace": "liqo-tenant-external-ovh-0001",
                "age_s": age_s, "unschedulable": True,
                "why": "0/8 nodes are available: 3 Too many pods"}

    def build_with_refusal(self, stuck=None):
        c = creds("external-ovh-0001", "external-ovh-0002")
        for cred in c.values():
            cred.pool = "default"
        hub = StubHub(credentials=c,
                      stuck_pods=(self.refused_pod(),) if stuck is None
                      else stuck)
        liqo = StubLiqo()
        def refuse(machine, kubeconfig):
            liqo.peered_calls.append(machine)
            if machine == "external-ovh-0001":
                raise PeeringBlocked("gw-x cannot be scheduled: Too many pods")
            liqo._peered.add(machine)
        liqo.peer = refuse
        ctrl, clock = build(hub, liqo=liqo)
        for n in c:
            fake(ctrl).machines[n] = _machine(n, clock())
        return ctrl, clock, liqo

    def test_a_refused_machine_is_not_retried_next_pass(self):
        ctrl, _, liqo = self.build_with_refusal()
        ctrl.reconcile()
        ctrl.reconcile()
        self.assertEqual(liqo.peered_calls.count("external-ovh-0001"), 1,
                         "retried a handshake the scheduler already refused")

    def test_the_machines_behind_it_still_get_through(self):
        """The backoff must not become its own blockage."""
        ctrl, _, liqo = self.build_with_refusal()
        ctrl.reconcile()
        self.assertIn("external-ovh-0002", liqo.peered_calls)

    def test_it_is_retried_once_the_backoff_expires(self):
        """The ceiling still holds even if the refusal never visibly lifts."""
        ctrl, clock, liqo = self.build_with_refusal()
        ctrl.reconcile()
        clock.advance(ctrl.cfg.liqo.blocked_retry_s + 1)
        ctrl.reconcile()
        self.assertEqual(liqo.peered_calls.count("external-ovh-0001"), 2)

    def test_it_is_retried_as_soon_as_the_refusal_lifts(self):
        """Add a hub node and the gateway pod is placed within seconds, but the
        operator sat out the rest of a ten-minute timer anyway. The clock was
        never the thing being waited for."""
        ctrl, clock, liqo = self.build_with_refusal()
        ctrl.reconcile()
        self.assertEqual(liqo.peered_calls.count("external-ovh-0001"), 1)

        ctrl.hub.stuck_pods = ()             # a node was added; the pod is placed
        clock.advance(15)                    # far short of blocked_retry_s
        ctrl.reconcile()
        self.assertEqual(liqo.peered_calls.count("external-ovh-0001"), 2,
                         "kept counting down after the refusal had lifted")

    def test_a_hub_that_cannot_be_read_does_not_release_the_backoff(self):
        """No refusals listed and no hub to list them are the same empty set and
        opposite facts. Releasing on an API error retries every blocked machine
        on the strength of a failed call."""
        ctrl, clock, liqo = self.build_with_refusal()
        ctrl.reconcile()
        ctrl.hub.stuck_pods = None           # the read RAISES
        clock.advance(15)
        ctrl.reconcile()
        self.assertEqual(liqo.peered_calls.count("external-ovh-0001"), 1)

    def test_a_pending_pod_with_no_verdict_does_not_hold_the_backoff(self):
        """Only a REFUSAL is evidence the hub is still saying no. A gateway pod
        Pending because it is pulling its image is the scheduler's job done --
        it was placed -- and holding the machine on that would wait out a
        ten-minute timer for a condition that never existed."""
        ctrl, clock, liqo = self.build_with_refusal()
        ctrl.reconcile()
        ctrl.hub.stuck_pods = ({**self.refused_pod(), "unschedulable": False,
                                "why": "no scheduling condition reported yet"},)
        clock.advance(15)
        ctrl.reconcile()
        self.assertEqual(liqo.peered_calls.count("external-ovh-0001"), 2)

    def test_another_machines_refusal_does_not_hold_this_one(self):
        """refused_now is keyed by machine, and a stale pod from a machine that
        has been reaped must not pin an unrelated one."""
        ctrl, clock, liqo = self.build_with_refusal()
        ctrl.reconcile()
        ctrl.hub.stuck_pods = ({**self.refused_pod(),
                                "name": "gw-external-ovh-9999",
                                "machine": "external-ovh-9999"},)
        clock.advance(15)
        ctrl.reconcile()
        self.assertEqual(liqo.peered_calls.count("external-ovh-0001"), 2)

    def test_a_reaped_machine_forgets_its_backoff(self):
        """Names are reused; a stale entry would silently skip a healthy box."""
        ctrl, _, _ = self.build_with_refusal()
        ctrl.reconcile()
        self.assertIn("external-ovh-0001", ctrl.blocked_until)
        ctrl.reap("external-ovh-0001", None, {}, set(), graceful=False)
        self.assertNotIn("external-ovh-0001", ctrl.blocked_until)


class TenantTerminatingIsNotAFailure(unittest.TestCase):
    """A 403 whose cause is NamespaceTerminating means a teardown for this
    machine got there first. Reported as "bring-up failed: (403) Forbidden" it
    reads as a broken ClusterRole, and the ClusterRole is fine."""

    def build_with(self, exc):
        c = creds("external-ovh-0001")
        c["external-ovh-0001"].pool = "default"
        hub = StubHub(credentials=c)
        liqo = StubLiqo(peered={"external-ovh-0001"})   # peered, not sliced
        def boom(*a, **kw):
            raise exc
        liqo.ensure_slice = boom
        ctrl, clock = build(hub, liqo=liqo)
        fake(ctrl).machines["external-ovh-0001"] = _machine(
            "external-ovh-0001", clock())
        return ctrl

    def test_it_is_not_logged_as_an_error(self):
        ctrl = self.build_with(
            TenantTerminating("liqo-tenant-external-ovh-0001"))
        with self.assertLogs("controller", level="INFO") as caught:
            ctrl.reconcile()
        out = "\n".join(caught.output)
        self.assertNotIn("bring-up failed", out)
        self.assertIn("terminating", out)

    def test_any_other_failure_is_still_an_error(self):
        ctrl = self.build_with(RuntimeError("connection refused"))
        with self.assertLogs("controller", level="ERROR") as caught:
            ctrl.reconcile()
        self.assertIn("bring-up failed", "\n".join(caught.output))


class StuckPodDiagnostic(unittest.TestCase):
    """Reported, never acted on. If this ever starts capping or skipping, it has
    stopped being a diagnostic."""

    STUCK = [{"name": "gw-external-ovh-0001",
              "namespace": "liqo-tenant-external-ovh-0001",
              "machine": "external-ovh-0001", "age_s": 613,
              "unschedulable": True,
              "why": "0/4 nodes are available: 1 Insufficient cpu"}]

    def test_it_is_logged_with_the_cause(self):
        hub = StubHub(stuck_pods=self.STUCK)
        ctrl, clock = build(hub, liqo=StubLiqo())
        fake(ctrl).machines["external-ovh-0001"] = _machine(
            "external-ovh-0001", clock())
        with self.assertLogs("controller", level="ERROR") as caught:
            ctrl.reconcile()
        out = "\n".join(caught.output)
        # The essentials, and only these: which machine, which pod, how long,
        # and the scheduler's own words. Anything else is a docstring's job.
        self.assertIn("external-ovh-0001", out)
        self.assertIn("gw-external-ovh-0001", out)
        self.assertIn("Insufficient cpu", out)

    def test_it_changes_nothing(self):
        c = creds("external-ovh-0001")
        c["external-ovh-0001"].pool = "default"
        hub = StubHub(demand=4, credentials=c, stuck_pods=self.STUCK)
        liqo = StubLiqo()
        ctrl, clock = build(hub, liqo=liqo)
        fake(ctrl).machines["external-ovh-0001"] = _machine(
            "external-ovh-0001", clock())
        ctrl.reconcile()
        self.assertEqual(liqo.peered_calls, ["external-ovh-0001"])
        self.assertTrue(fake(ctrl).created, "scale-up was capped")

    def test_residue_from_a_reaped_machine_is_not_repeated_forever(self):
        """A stuck gateway pod outlives the machine that caused it. Saying so
        every pass, indefinitely, trains the reader to skip the line -- and the
        next real one goes with it. force_teardown owns removing the residue."""
        hub = StubHub(stuck_pods=self.STUCK)     # no machine by that name
        ctrl, _ = build(hub, liqo=StubLiqo())
        with self.assertLogs("controller", level="INFO") as caught:
            ctrl.reconcile()
        self.assertNotIn("CANNOT BE SCHEDULED", "\n".join(caught.output))

    def test_a_healthy_hub_says_nothing(self):
        ctrl, _ = build(StubHub(), liqo=StubLiqo())
        with self.assertLogs("controller", level="INFO") as caught:
            ctrl.reconcile()
        self.assertNotIn("CANNOT BE SCHEDULED", "\n".join(caught.output))

    def test_an_unreadable_hub_is_a_warning_not_a_dead_pass(self):
        c = creds("external-ovh-0001")
        c["external-ovh-0001"].pool = "default"
        hub = StubHub(credentials=c, stuck_pods=None)    # the read raises
        liqo = StubLiqo()
        ctrl, clock = build(hub, liqo=liqo)
        fake(ctrl).machines["external-ovh-0001"] = _machine(
            "external-ovh-0001", clock())
        ctrl.reconcile()
        self.assertEqual(liqo.peered_calls, ["external-ovh-0001"])


def _machine(name, created, ip="198.51.100.9"):
    from autoscaler.providers.base import Machine
    return Machine(name=name, id=f"id-{name}", status="ACTIVE", created=created,
                   ip=ip)


def creds(*machines):
    """Real Credentials rather than a sentinel: the controller composes a
    kubeconfig from these plus the provider's address every pass."""
    from autoscaler import pki
    return {m: pki.generate(m)[1] for m in machines}


def _raise(msg):
    def boom(*a, **kw):
        raise RuntimeError(msg)
    return boom


if __name__ == "__main__":
    unittest.main()
