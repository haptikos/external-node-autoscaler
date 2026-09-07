"""Direct tests for Hub.stuck_peering_pods.

It had none. Every existing test reached it through StubHub, which reimplements
the method -- so the production walk (which namespaces to open, which pods to
report, what a failed read means) was covered only by a manual check against a
live cluster. This is the read the peering backoff is decided on: a pod it fails
to report is a refusal that silently lifts.
"""
import unittest

from kubernetes import client as k8s

from support import (StubCore, make_config, plain_ns, pod,  # noqa: E402
                     tenant_ns)

from autoscaler.hub import Hub                             # noqa: E402


NOW = 2_000_000.0


def gw(machine, age_s=60, why=None, phase="Pending", name=None):
    return pod(name=name or f"gw-{machine}", ns=f"liqo-tenant-{machine}",
               phase=phase, gpus=0, created=NOW - age_s,
               unschedulable=why is not None, why=why)


class StuckPeeringPods(unittest.TestCase):
    def hub(self, pods=(), namespaces=None, pod_errors=None):
        if namespaces is None:
            namespaces = tuple(
                tenant_ns(ns) for ns in
                dict.fromkeys(p.metadata.namespace for p in pods))
        core = StubCore(pods=pods, namespaces=namespaces,
                        pod_errors=pod_errors)
        hub = Hub(core, None, make_config())
        hub._now = NOW
        return hub

    def setUp(self):
        # Freeze the clock the method reads, so ages are the numbers written
        # in the fixtures rather than whatever wall time it is.
        import autoscaler.hub as hub_mod
        self._real = hub_mod.time.time
        hub_mod.time.time = lambda: NOW
        self.addCleanup(setattr, hub_mod.time, "time", self._real)

    def test_it_reports_a_refused_gateway_pod(self):
        hub = self.hub([gw("external-ovh-0001", why="3 Too many pods")])
        [p] = hub.stuck_peering_pods(0)
        self.assertEqual(p["machine"], "external-ovh-0001")
        self.assertEqual(p["namespace"], "liqo-tenant-external-ovh-0001")
        self.assertTrue(p["unschedulable"])
        self.assertEqual(p["why"], "3 Too many pods")
        self.assertEqual(p["age_s"], 60)

    def test_a_pending_pod_with_no_verdict_is_reported_as_unruled(self):
        hub = self.hub([gw("external-ovh-0001")])
        [p] = hub.stuck_peering_pods(0)
        self.assertFalse(p["unschedulable"])
        self.assertIn("no scheduling condition", p["why"])

    def test_running_pods_are_not_stuck(self):
        hub = self.hub([gw("external-ovh-0001", phase="Running")])
        self.assertEqual(hub.stuck_peering_pods(0), [])

    def test_min_age_is_honoured(self):
        hub = self.hub([gw("external-ovh-0001", age_s=30)])
        self.assertEqual(len(hub.stuck_peering_pods(0)), 1)
        self.assertEqual(hub.stuck_peering_pods(300), [])

    def test_somebody_elses_peering_is_not_ours_to_diagnose(self):
        """A hand-managed cluster peered by someone else has tenant namespaces
        too. Acting on those would tear down peerings we do not own."""
        hub = self.hub([gw("partner-cluster", why="3 Too many pods")])
        self.assertEqual(hub.stuck_peering_pods(0), [])

    def test_only_tenant_namespaces_are_opened(self):
        """The scoping is the label, not the pod list. A Pending pod elsewhere
        in the cluster -- kube-system, the workload namespace -- must never
        reach the peering backoff."""
        noise = pod(name="some-app", ns="inference", phase="Pending", gpus=0,
                    created=NOW - 600, unschedulable=True, why="Insufficient cpu")
        hub = self.hub([noise, gw("external-ovh-0001", why="3 Too many pods")],
                       namespaces=(tenant_ns("liqo-tenant-external-ovh-0001"),))
        [p] = hub.stuck_peering_pods(0)
        self.assertEqual(p["name"], "gw-external-ovh-0001")

    def test_a_namespace_outliving_its_machine_is_still_read(self):
        """This is what a leaked peering looks like, and it is the case a
        fleet-driven namespace list would be structurally blind to."""
        hub = self.hub([gw("external-ovh-dead", why="3 Too many pods")])
        [p] = hub.stuck_peering_pods(0)
        self.assertEqual(p["machine"], "external-ovh-dead")

    def test_the_owner_comes_from_liqos_label_not_the_name(self):
        """The label is Liqo stating the owner; the name is our inference."""
        hub = self.hub(
            [gw("external-ovh-0001", why="3 Too many pods")],
            namespaces=(tenant_ns("liqo-tenant-external-ovh-0001",
                                  machine="external-ovh-renamed"),))
        [p] = hub.stuck_peering_pods(0)
        self.assertEqual(p["machine"], "external-ovh-renamed")

    def test_an_unlabelled_namespace_is_skipped_out_loud(self):
        """Liqo stamps the owner on every tenant namespace, so this should not
        happen. If it ever does, it must not pass silently: this read decides
        whether a peering backoff is released, and a namespace nobody can
        attribute is a refusal nobody read."""
        hub = self.hub(
            [gw("external-ovh-0001", why="3 Too many pods")],
            namespaces=(tenant_ns("liqo-tenant-external-ovh-0001",
                                  labelled=False),))
        with self.assertLogs("hub", level="WARNING") as caught:
            self.assertEqual(hub.stuck_peering_pods(0), [])
        self.assertIn("liqo.io/remote-cluster-id", "\n".join(caught.output))

    def test_a_namespace_deleted_mid_read_is_not_an_error(self):
        """Teardown deletes these under us constantly. 404 means there are no
        pods there, which is the true answer."""
        hub = self.hub([gw("external-ovh-0001", why="3 Too many pods")],
                       pod_errors={"liqo-tenant-external-ovh-0001": 404})
        self.assertEqual(hub.stuck_peering_pods(0), [])

    def test_any_other_pod_read_failure_propagates(self):
        """An incomplete read must never be reported as an empty one: the
        caller releases peering backoffs on empty, so swallowing a 403 here
        retries every blocked machine on the strength of a failed call."""
        hub = self.hub([gw("external-ovh-0001", why="3 Too many pods")],
                       pod_errors={"liqo-tenant-external-ovh-0001": 403})
        with self.assertRaises(k8s.exceptions.ApiException):
            hub.stuck_peering_pods(0)

    # -- carried over from test_controller.StuckPeeringPods, which tested this
    # -- hub method from the controller's file. Same coverage, kept verbatim
    # -- apart from the namespaces the walk now needs.

    def test_a_pod_younger_than_the_threshold_is_not_stuck_yet(self):
        """Every gateway pod is Pending for a moment. Reporting that would make
        the line noise, and noise is how a real one gets missed."""
        hub = self.hub([gw("external-ovh-0001", age_s=5,
                           why="1 Insufficient cpu")])
        self.assertEqual(hub.stuck_peering_pods(120), [])

    def test_a_workload_namespace_is_never_opened(self):
        """The same reason virtual_nodes() filters on the name prefix."""
        pods = [gw("handmanaged-a10", why="1 Insufficient cpu"),
                pod(name="other", ns="inference", phase="Pending", gpus=0,
                    unschedulable=True, created=NOW - 600)]
        hub = self.hub(pods,
                       namespaces=(tenant_ns("liqo-tenant-handmanaged-a10"),))
        self.assertEqual(hub.stuck_peering_pods(0), [])

    def test_a_namespace_merely_NAMED_like_a_tenant_is_not_one(self):
        """The scoping is Liqo's label, not the name. A Pending pod in a
        hand-made `liqo-tenant-*` namespace would otherwise read as a refusal
        and pin a backoff against a peering that does not exist. Drop the
        selector and the name prefix alone still lets it through.
        """
        impostor = pod(name="gw-external-ovh-fake",
                       ns="liqo-tenant-external-ovh-fake", phase="Pending",
                       gpus=0, created=NOW - 600, unschedulable=True,
                       why="3 Too many pods")
        hub = self.hub([impostor],
                       namespaces=(plain_ns("liqo-tenant-external-ovh-fake"),))
        self.assertEqual(hub.stuck_peering_pods(0), [])

    def test_a_pod_with_no_timestamp_is_treated_as_brand_new(self):
        """The reading that does NOT produce a false alarm."""
        p = pod(name="gw-x", ns="liqo-tenant-external-ovh-0001",
                phase="Pending", gpus=0, unschedulable=True, why="Too many pods")
        hub = self.hub([p])
        self.assertEqual(hub.stuck_peering_pods(1), [])

    def test_every_tenant_namespace_is_read(self):
        hub = self.hub([gw("external-ovh-0001", why="3 Too many pods"),
                        gw("external-ovh-0002", why="3 Too many pods")])
        self.assertEqual({p["machine"] for p in hub.stuck_peering_pods(0)},
                         {"external-ovh-0001", "external-ovh-0002"})


if __name__ == "__main__":
    unittest.main()
