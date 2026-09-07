"""The remote-client settings, asserted on the object that enforces them.

Written the obvious way first, it was a silent no-op: the value looked right on
the client and urllib3 went on doing what it liked. So these reach into the
constructed pool rather than reading back the configuration we just set.
"""
import os
import subprocess
import sys
import tempfile
import types
import unittest

from support import make_config          # noqa: F401  (sys.path bootstrap)

from autoscaler import pki
from autoscaler.liqo import Liqo, PeeringBlocked


def kubeconfig_file():
    _, creds = pki.generate("external-ovh-0001")
    fh = tempfile.NamedTemporaryFile("wb", suffix=".kubeconfig", delete=False)
    fh.write(creds.kubeconfig("198.51.100.1"))
    fh.close()
    return fh.name


class RemoteClientTest(unittest.TestCase):
    def setUp(self):
        self.path = kubeconfig_file()
        self.addCleanup(os.unlink, self.path)
        # Liqo reads crd/core off the hub at construction; nothing below calls
        # either, so the hub is only there to satisfy that.
        self.liqo = Liqo(types.SimpleNamespace(crd=None, core=None),
                         make_config())

    def test_urllib3_does_not_retry_a_probe(self):
        """A booting machine DROPs, so every attempt costs the full timeout and
        urllib3's default turns one 5s probe into ~20s."""
        cli = self.liqo._remote_client(self.path)
        retries = cli.rest_client.pool_manager.connection_pool_kw.get("retries")
        # urllib3 normalises an int into a Retry, so read the budget rather than
        # comparing to what we passed in.
        total = getattr(retries, "total", retries)
        self.assertEqual(
            total, 0,
            f"retries={retries!r} reached the pool. It must be set on the "
            f"Configuration BEFORE ApiClient builds its RESTClientObject; "
            f"assigning client.configuration.retries afterwards is a no-op.")

    def test_the_machines_own_ca_is_what_verifies_it(self):
        """Not the system trust store, and not skipped."""
        cli = self.liqo._remote_client(self.path)
        self.assertTrue(cli.configuration.verify_ssl)
        self.assertTrue(cli.configuration.ssl_ca_cert)

    def test_kubeconfig_is_loaded_rather_than_ignored(self):
        cli = self.liqo._remote_client(self.path)
        self.assertEqual(cli.configuration.host, "https://198.51.100.1:6443")
        self.assertTrue(cli.configuration.cert_file)
        self.assertTrue(cli.configuration.key_file)


if __name__ == "__main__":
    unittest.main()


class WatchedRun(unittest.TestCase):
    """`liqoctl peer` can block for its whole timeout, and this loop is
    single-threaded. Without a hook that runs DURING the wait, any explanation
    of the hang arrives only once the hang is over -- which is exactly the ten
    minutes it was meant to explain.
    """

    def liqo(self, hub=None):
        return Liqo(hub or types.SimpleNamespace(crd=None, core=None),
                    make_config())

    def test_the_hook_runs_while_the_command_is_still_running(self):
        liqo = self.liqo()
        liqo.WATCH_INTERVAL_S = 0.05
        calls = []
        r = liqo._run([sys.executable, "-c", "import time; time.sleep(0.35)"],
                      timeout=10, on_wait=lambda: calls.append(1))
        self.assertEqual(r.returncode, 0)
        self.assertGreaterEqual(len(calls), 2,
                                "the wait was never interrupted to look")

    def test_output_survives_the_polling(self):
        """communicate() is retried after each TimeoutExpired, which is
        documented as lossless -- assert it, because losing liqoctl's tail would
        cost the error message we are here for."""
        liqo = self.liqo()
        liqo.WATCH_INTERVAL_S = 0.05
        r = liqo._run(
            [sys.executable, "-c",
             "import sys,time; time.sleep(0.2); sys.stdout.write('out'); "
             "sys.stderr.write('err'); sys.exit(3)"],
            timeout=10, on_wait=lambda: None)
        self.assertEqual((r.returncode, r.stdout, r.stderr), (3, "out", "err"))

    def test_a_broken_hook_does_not_break_the_command(self):
        """The watcher only narrates. It must never be able to fail a peering
        that was going to succeed."""
        liqo = self.liqo()
        liqo.WATCH_INTERVAL_S = 0.05
        def boom():
            raise RuntimeError("api down")
        r = liqo._run([sys.executable, "-c", "import time; time.sleep(0.2)"],
                      timeout=10, on_wait=boom)
        self.assertEqual(r.returncode, 0)

    def test_the_timeout_still_bites(self):
        liqo = self.liqo()
        liqo.WATCH_INTERVAL_S = 0.05
        with self.assertRaises(subprocess.TimeoutExpired):
            liqo._run([sys.executable, "-c", "import time; time.sleep(30)"],
                      timeout=0.2, on_wait=lambda: None)

    def test_no_hook_means_no_polling_at_all(self):
        """Every other liqoctl call keeps the plain path."""
        liqo = self.liqo()
        r = liqo._run([sys.executable, "-c", "print('hi')"], timeout=10)
        self.assertEqual(r.stdout.strip(), "hi")


class BlockedPeeringReport(unittest.TestCase):
    #: Pending, but the scheduler has not ruled -- reported, never aborted on.
    STUCK = {"name": "gw-external-ovh-0001", "machine": "external-ovh-0001",
             "namespace": "liqo-tenant-external-ovh-0001", "age_s": 400,
             "unschedulable": False,
             "why": "no scheduling condition reported yet"}
    #: PodScheduled=False: the scheduler tried and refused.
    REFUSED = {**STUCK, "unschedulable": True,
               "why": "0/9 nodes are available: 3 Too many pods"}

    def liqo_with(self, *pods):
        hub = types.SimpleNamespace(
            crd=None, core=None,
            stuck_peering_pods=lambda min_age_s: list(pods))
        return Liqo(hub, make_config())

    def test_it_names_the_pod_and_the_schedulers_reason(self):
        liqo = self.liqo_with(self.STUCK)
        with self.assertLogs("liqo", level="ERROR") as caught:
            liqo.report_blocked("external-ovh-0001", set())
        out = "\n".join(caught.output)
        self.assertIn("gw-external-ovh-0001", out)
        self.assertIn(self.STUCK["why"], out)

    def test_it_speaks_once_per_handshake(self):
        """It is called every WATCH_INTERVAL_S. Twenty copies of one line would
        bury the peerings around it."""
        liqo = self.liqo_with(self.STUCK)
        said = set()
        with self.assertLogs("liqo", level="ERROR"):
            liqo.report_blocked("external-ovh-0001", said)
        with self.assertRaises(AssertionError):      # nothing logged the 2nd time
            with self.assertLogs("liqo", level="ERROR"):
                liqo.report_blocked("external-ovh-0001", said)

    def test_another_machines_stuck_pod_is_not_this_machines_problem(self):
        """Reporting it here would blame the handshake that happens to be
        running for a pod left behind by an earlier one."""
        liqo = self.liqo_with(self.STUCK)
        with self.assertRaises(AssertionError):
            with self.assertLogs("liqo", level="ERROR"):
                liqo.report_blocked("external-ovh-0002", set())


class ARefusalIsActedOnAtOnce(unittest.TestCase):
    """The abort path has no clock on it, and that is the point.

    report_blocked used to ask for pods older than stuck_pod_s, so a verdict
    written at second two stayed invisible until second 120. What separates the
    two pods below is not age -- they are the same age -- but whether the
    scheduler has ruled.
    """

    #: Refused, and far younger than stuck_pod_s. The old code could not see
    #: this pod at all; it is the one that must be actionable immediately.
    YOUNG_REFUSED = {"name": "gw-external-ovh-0001",
                     "machine": "external-ovh-0001",
                     "namespace": "liqo-tenant-external-ovh-0001",
                     "age_s": 20, "unschedulable": True,
                     "why": "0/8 nodes are available: 3 Too many pods"}
    #: Same pod, same age, no verdict -- an image still pulling looks exactly
    #: like this. Age alone cannot tell it from the one above.
    YOUNG_UNRULED = {**YOUNG_REFUSED, "unschedulable": False,
                     "why": "no scheduling condition reported yet"}

    def liqo(self, pod, **liqo_cfg):
        """A hub that HONOURS min_age_s, like the real one, so the age the code
        asks for is load-bearing rather than decorative."""
        self.asked = []
        def stuck(min_age_s):
            self.asked.append(min_age_s)
            return [pod] if pod["age_s"] >= min_age_s else []
        hub = types.SimpleNamespace(crd=None, core=None,
                                    stuck_peering_pods=stuck)
        return Liqo(hub, make_config(liqo=liqo_cfg))

    def test_a_refusal_is_acted_on_without_waiting_out_stuck_pod_seconds(self):
        liqo = self.liqo(self.YOUNG_REFUSED)
        self.assertLess(self.YOUNG_REFUSED["age_s"], liqo.cfg.liqo.stuck_pod_s)
        with self.assertLogs("liqo", level="ERROR"):
            with self.assertRaises(PeeringBlocked):
                liqo.report_blocked("external-ovh-0001", set())

    def test_the_query_does_not_filter_by_age(self):
        """Any lower bound on the query silently reintroduces the delay: a
        refusal younger than it never reaches the code that acts on it."""
        liqo = self.liqo(self.YOUNG_REFUSED)
        with self.assertLogs("liqo", level="ERROR"):
            with self.assertRaises(PeeringBlocked):
                liqo.report_blocked("external-ovh-0001", set())
        self.assertEqual(self.asked, [0])

    def test_the_same_pod_unruled_is_left_alone_at_that_age(self):
        """An image-pulling gateway pod is Pending too. Aborting on age would
        kill it and then sit on a blocked_retry_s backoff for nothing."""
        liqo = self.liqo(self.YOUNG_UNRULED)
        with self.assertRaises(AssertionError):          # nothing said
            with self.assertLogs("liqo", level="ERROR"):
                liqo.report_blocked("external-ovh-0001", set())

    def test_an_unruled_pod_past_stuck_pod_seconds_is_reported_not_aborted(self):
        """Time is the only evidence there is when no verdict exists, so it
        still buys a report -- but never a kill."""
        liqo = self.liqo({**self.YOUNG_UNRULED, "age_s": 400})
        with self.assertLogs("liqo", level="ERROR") as caught:
            liqo.report_blocked("external-ovh-0001", set())      # no raise
        self.assertIn(self.YOUNG_UNRULED["why"], "\n".join(caught.output))

    def test_the_report_threshold_is_generous_enough_to_mean_something(self):
        """Nothing acts on stuck_pod_s, so its job is to stay quiet until
        something is genuinely wrong. Set near a normal image pull it becomes
        noise, and the line stops being read."""
        self.assertGreaterEqual(make_config().liqo.stuck_pod_s, 300)

    def test_the_watch_tick_is_the_only_latency_left(self):
        """With no grace period, a doomed handshake runs for at most one tick
        after the scheduler condemns it."""
        self.assertLessEqual(Liqo.WATCH_INTERVAL_S, 10)


class PeerIsWatched(unittest.TestCase):
    """The wiring, which is what makes any of the above reach a human. Removing
    `on_wait` from peer() leaves every test above green while the ten-minute
    silence comes straight back."""

    def test_peer_passes_a_watcher_that_reports_this_machine(self):
        seen = {}
        hub = types.SimpleNamespace(
            crd=types.SimpleNamespace(
                patch_cluster_custom_object=lambda *a, **k: None),
            core=None,
            stuck_peering_pods=lambda min_age_s: [
                {"name": "gw-external-ovh-0001",
                 "machine": "external-ovh-0001",
                 "namespace": "liqo-tenant-external-ovh-0001",
                 "age_s": 400, "unschedulable": False,
                 "why": "1 Insufficient cpu"}])
        liqo = Liqo(hub, make_config())
        liqo.liqoctl = lambda args, **kw: seen.update(kw)

        _, creds = pki.generate("external-ovh-0001")
        liqo.peer("external-ovh-0001", creds.kubeconfig("198.51.100.1"))

        self.assertIsNotNone(seen.get("on_wait"),
                             "peer() no longer watches its own handshake")
        with self.assertLogs("liqo", level="ERROR") as caught:
            seen["on_wait"]()
        self.assertIn("1 Insufficient cpu", "\n".join(caught.output))


class AbandonsARefusedHandshake(unittest.TestCase):
    """Waiting out peer_timeout cannot change a verdict the scheduler has
    already given. The pod is not queued behind anything -- it was rejected."""

    def liqo(self, pod, abort=True):
        hub = types.SimpleNamespace(
            crd=None, core=None,
            stuck_peering_pods=lambda min_age_s: [pod])
        cfg = make_config(liqo={"abort_blocked_peering": abort})
        return Liqo(hub, cfg)

    def test_a_refused_pod_stops_the_wait(self):
        liqo = self.liqo(BlockedPeeringReport.REFUSED)
        with self.assertLogs("liqo", level="ERROR"):
            with self.assertRaises(PeeringBlocked):
                liqo.report_blocked("external-ovh-0001", set())

    def test_a_pod_the_scheduler_has_not_ruled_on_is_only_reported(self):
        """It might still be placed. Killing it would be guessing."""
        liqo = self.liqo(BlockedPeeringReport.STUCK)
        with self.assertLogs("liqo", level="ERROR"):
            liqo.report_blocked("external-ovh-0001", set())      # no raise

    def test_the_abort_can_be_turned_off(self):
        liqo = self.liqo(BlockedPeeringReport.REFUSED, abort=False)
        with self.assertLogs("liqo", level="ERROR"):
            liqo.report_blocked("external-ovh-0001", set())      # no raise

    def test_it_still_speaks_before_it_raises(self):
        """Raising silently would trade a ten-minute wait for a bare
        'exit 1' -- losing the scheduler's reason, which is the whole point."""
        liqo = self.liqo(BlockedPeeringReport.REFUSED)
        with self.assertLogs("liqo", level="ERROR") as caught:
            with self.assertRaises(PeeringBlocked):
                liqo.report_blocked("external-ovh-0001", set())
        self.assertIn("Too many pods", "\n".join(caught.output))

    def test_the_process_is_killed_rather_than_left_running(self):
        """_run must not leave liqoctl orphaned when the watcher gives up."""
        liqo = Liqo(types.SimpleNamespace(crd=None, core=None), make_config())
        liqo.WATCH_INTERVAL_S = 0.05
        def refuse():
            raise PeeringBlocked("nope")
        with self.assertRaises(PeeringBlocked):
            liqo._run([sys.executable, "-c", "import time; time.sleep(30)"],
                      timeout=30, on_wait=refuse)
