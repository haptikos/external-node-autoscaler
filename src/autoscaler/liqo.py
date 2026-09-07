"""Peering: liqoctl, ResourceSlices, and the teardown that has to work blind.

Two things to know before changing anything here:

  * liqoctl runs as a subprocess. `liqoctl peer` is three phases (network
    connect, authenticate, resource slice) and every one needs both kubeconfigs
    open at once; reimplementing the WireGuard key exchange and the
    Tenant/Identity CSR flow in Python would buy nothing but bugs.
  * We pass --create-resource-slice=false and forge the slice ourselves: only
    then can it advertise the GPU. liqoctl exposes --cpu/--memory/--pods and no
    way to request an extended resource.
"""
import contextlib
import os
import re
import subprocess
import time
import tempfile

from kubernetes import client as k8s, config as k8s_config

from . import logging_setup
from . import resources
from .resources import Resources
from .labels import (BOOTSTRAP_DONE, BOOTSTRAP_LABEL, MANAGED, POOL_LABEL,
                     TENANT_NS_PREFIX)

log = logging_setup.get("liqo")


class TenantTerminating(RuntimeError):
    """The tenant namespace is being deleted, so nothing can be created in it.
    Not a permission problem however much the 403 looks like one: a teardown is
    already under way."""


class PeeringBlocked(RuntimeError):
    """The handshake cannot finish, established while it is still running. The
    one exception _run does NOT swallow from `on_wait`, so a broken watcher can
    never fail a peering that was going to succeed."""

FC = ("core.liqo.io", "v1beta1", "foreignclusters")
RS = ("authentication.liqo.io", "v1beta1", "resourceslices")
VN = ("offloading.liqo.io", "v1beta1", "virtualnodes")
NM = ("offloading.liqo.io", "v1beta1", "namespacemaps")

_ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")


def _same_taints(have, want):
    """Order-insensitive, and ignores keys the API server defaults in
    (timeAdded), so a patch is issued only for a real difference."""
    def key(t):
        return (t.get("key"), t.get("value", ""), t.get("effect"))
    return sorted(map(key, have)) == sorted(map(key, want))


def tenant_ns(machine):
    """Liqo derives the tenant namespace from the provider cluster id, and the
    provider cluster id is the machine name."""
    return f"{TENANT_NS_PREFIX}{machine}"


def _clean_output(text):
    """Strip spinner redraws so the real message survives.

    liqoctl animates progress with ANSI escapes and carriage returns; left
    alone, a single logical line arrives as dozens of near-identical frames."""
    text = _ANSI.sub("", text).replace("\r", "\n").replace("\x08", "")
    out = []
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.strip() and (not out or out[-1] != line):
            out.append(line)
    return out


@contextlib.contextmanager
def as_file(kubeconfig):
    """liqoctl and the k8s client both want a path, not bytes."""
    with tempfile.NamedTemporaryFile("wb", suffix=".yaml", delete=False) as fh:
        fh.write(kubeconfig)
        path = fh.name
    try:
        yield path
    finally:
        os.unlink(path)


class Liqo:
    def __init__(self, hub, cfg):
        self.hub = hub
        self.cfg = cfg
        self.crd = hub.crd
        self.core = hub.core
        self.kubeconfig = None       # materialised on first use, see start()

    def start(self):
        self.kubeconfig = self.hub.kubeconfig_path()

    # -------------------------------------------------------------- liqoctl --
    def _run(self, cmd, timeout, on_wait=None):
        """subprocess.run, plus a heartbeat. Same capture, same timeout."""
        if on_wait is None:
            return subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=timeout, check=False)
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
        deadline = time.monotonic() + timeout
        while True:
            try:
                out, err = proc.communicate(timeout=self.WATCH_INTERVAL_S)
            except subprocess.TimeoutExpired:
                if time.monotonic() >= deadline:
                    proc.kill()
                    proc.communicate()
                    raise
                try:
                    on_wait()
                except PeeringBlocked:
                    # The one exception that IS a verdict. liqoctl exits
                    # non-zero either way and tears nothing down, so killing
                    # here gives up only the waiting.
                    proc.kill()
                    proc.communicate()
                    raise
                except Exception as e:                          # noqa: BLE001
                    # A broken watcher must never take down a peering it was
                    # only meant to narrate.
                    log.warning("peering watch failed: %s", e)
                continue
            return subprocess.CompletedProcess(cmd, proc.returncode, out, err)

    #: How often liqoctl's wait is interrupted to run `on_wait`. Nothing else
    #: delays the abort, so this IS its latency. One list call per tick.
    WATCH_INTERVAL_S = 10

    def liqoctl(self, args, remote_kubeconfig=None, timeout=900, on_wait=None):
        """Run liqoctl against the hub, optionally with a remote cluster.

        `on_wait` is called every WATCH_INTERVAL_S while liqoctl runs. The loop
        is single-threaded, so without it any diagnosis of a stalled peering has
        to wait out the very timeout it explains. Retrying communicate() after a
        TimeoutExpired is documented as lossless, so the polling costs no
        output.
        """
        cmd = [self.cfg.liqo.liqoctl_bin, *args,
               "--kubeconfig", self.kubeconfig,
               "--namespace", self.cfg.liqo.namespace]
        if remote_kubeconfig:
            cmd += ["--remote-kubeconfig", remote_kubeconfig,
                    "--remote-namespace", self.cfg.liqo.namespace]
        r = self._run(cmd, timeout, on_wait)
        if r.returncode != 0:
            # Include the flags: a wrong one fails identically to a real peering
            # error, and the message alone does not say which was attempted.
            redacted = " ".join(
                c if c != remote_kubeconfig else "<remote-kubeconfig>"
                for c in cmd)
            lines = _clean_output((r.stderr or "") + "\n" + (r.stdout or ""))
            # The error is the LAST thing liqoctl prints, after pages of spinner
            # redraws. Truncating the head shows the banner and hides the failure.
            log.error("liqoctl failed: %s", redacted)
            for line in lines[-40:]:
                log.error("    | %s", line)
            raise RuntimeError(f"exit {r.returncode}: "
                               f"{' / '.join(lines[-3:]) or '(no output)'}")
        return r.stdout

    def check_version(self):
        """Fail loudly at startup if the bundled liqoctl is not the pinned version.

        The version must match the Liqo chart on the hub AND the one each machine
        installs. A mismatch does not fail here — it fails deep inside a peering,
        minutes later, on a machine that is already costing money.
        """
        want = self.cfg.liqo.version
        try:
            out = subprocess.run([self.cfg.liqo.liqoctl_bin, "version", "--client"],
                                 capture_output=True, text=True, timeout=30,
                                 check=False).stdout
        except Exception as e:                                  # noqa: BLE001
            log.warning("could not run %s: %s", self.cfg.liqo.liqoctl_bin, e)
            return
        if want not in out:
            log.warning("liqoctl reports %r, expected %s. Peering across a "
                        "version skew fails late and confusingly.",
                        out.strip(), want)

    # ---------------------------------------------------------------- reads --
    def machine_ready(self, machine, kubeconfig):
        """THE GATE IN FRONT OF peer(). Two checks, one API call: the Node is
        Ready, and it carries BOOTSTRAP_LABEL.

        The label is not belt-and-braces. Peering before the device plugin lands
        reads zero allocatable units, writes a ResourceSlice advertising zero
        GPUs, and nothing repairs it.

        Bounded hard: a machine mid-boot has ufw set to DROP, so an unbounded
        connect hangs this single-threaded loop. Anything wrong means "not
        ready", never an exception; boot_s decides between booting and dead.
        """
        try:
            with as_file(kubeconfig) as path:
                nodes = k8s.CoreV1Api(self._remote_client(path)).list_node(
                    _request_timeout=self.cfg.probe_timeout_s).items
        except Exception as e:                                  # noqa: BLE001
            log.debug("%s: not reachable yet (%s)", machine, e)
            return False
        if not nodes:
            return False
        node = nodes[0]
        ready = any(c.type == "Ready" and c.status == "True"
                    for c in (node.status.conditions or []))
        stamped = (node.metadata.labels or {}).get(
            BOOTSTRAP_LABEL) == BOOTSTRAP_DONE
        if not (ready and stamped):
            log.info("%s: not ready yet (node ready=%s, bootstrap complete=%s)",
                     machine, ready, stamped)
        return ready and stamped

    def _remote_client(self, path):
        """A client for a rented machine, with the timeouts a WAN call needs.

        `retries` HAS TO BE SET ON THE CONFIGURATION BEFORE THE CLIENT IS BUILT:
        ApiClient bakes it into the urllib3 PoolManager in __init__, so
        assigning client.configuration.retries afterwards is a silent no-op that
        turns one 5s timeout into four attempts. tests/test_liqo.py asserts the
        pool itself.
        """
        conf = type.__call__(k8s.Configuration)
        conf.retries = 0
        return k8s_config.new_client_from_config(path, client_configuration=conf)

    def peerings(self):
        """Cluster ids we have peered. Cluster id == machine name by construction."""
        items = self.crd.list_cluster_custom_object(*FC[:2], FC[2])["items"]
        return {i["metadata"]["name"] for i in items
                if i["metadata"].get("labels", {}).get("managed-by") == MANAGED}

    def slices(self):
        """Machines that already have a ResourceSlice.

        Tracked separately from peerings() on purpose. The two halves of
        bringing a machine online -- the liqoctl handshake and the slice that
        advertises its capacity -- fail independently, so the loop has to
        converge on both.
        """
        items = self.crd.list_cluster_custom_object(
            *RS[:2], RS[2], label_selector=f"managed-by={MANAGED}")["items"]
        return {i["metadata"]["name"] for i in items}

    def remote_capacity(self, kubeconfig_path):
        """The machine's single node, as a vector, so the slice advertises what
        is actually there rather than what a flavor table guessed."""
        nodes = k8s.CoreV1Api(
            self._remote_client(kubeconfig_path)).list_node(
                _request_timeout=self.cfg.probe_timeout_s).items
        if not nodes:
            raise RuntimeError("machine reports no nodes yet")
        return Resources.from_api(nodes[0].status.allocatable or {})

    # --------------------------------------------------------------- writes --
    def create_slice(self, machine, pool, capacity):
        """Forge the ResourceSlice ourselves: up to Liqo v1.1, liqoctl could
        request only cpu/memory/pods, so a liqoctl-created slice yielded a
        virtual node with no GPU on it. v1.2.0 added `liqoctl peer --resource`,
        which may make this unnecessary -- not evaluated yet.

        Advertises the machine's whole schedulable vector, so every dimension
        the scheduler might bind on is present. A dimension left out is a
        dimension the scheduler will happily oversubscribe.
        """
        schedulable = pool.schedulable(capacity)
        res = {}
        for name in schedulable.keys():
            v = schedulable[name]
            if name == resources.CPU:
                # Floors of 1: a slice advertising nothing is useless. On a
                # small machine the floor BECOMES the number, so keep it low
                # enough not to override the reserve it is protecting.
                res["cpu"] = f"{max(1, v)}m"
            elif name == resources.MEMORY:
                res["memory"] = f"{max(1, v // (1024 ** 2))}Mi"
            else:
                res[name] = str(max(1, v))
        res.setdefault("pods", str(self.cfg.liqo.max_pods_per_machine))

        body = {
            "apiVersion": "authentication.liqo.io/v1beta1",
            "kind": "ResourceSlice",
            "metadata": {
                "name": machine, "namespace": tenant_ns(machine),
                "annotations": {"liqo.io/create-virtual-node": "true"},
                "labels": {"liqo.io/remote-cluster-id": machine,
                           "liqo.io/remoteID": machine,
                           "liqo.io/replication": "true",
                           "managed-by": MANAGED}},
            "spec": {"class": "default",
                     "consumerClusterID": self.cfg.liqo.consumer_id,
                     "providerClusterID": machine,
                     "resources": res}}
        self.crd.create_namespaced_custom_object(
            *RS[:2], tenant_ns(machine), RS[2], body)

    def peer(self, machine, kubeconfig):
        """Networking + authentication handshake. No node pinning anywhere: the
        provider cluster has exactly one node, so its virtual node can only mean
        that box."""
        with as_file(kubeconfig) as path:
            log.info("peering %s", machine)
            said = set()
            self.liqoctl(["peer", "--gw-server-service-type", "NodePort",
                          "--create-resource-slice=false",
                          "--timeout", self.cfg.liqo.peer_timeout],
                         remote_kubeconfig=path,
                         on_wait=lambda: self.report_blocked(machine, said))
            # Stamp ownership immediately: force_teardown() finds peerings by
            # this label, and an unlabelled one would leak silently. The slice
            # is created in a separate step precisely BECAUSE this label lands
            # first.
            self.crd.patch_cluster_custom_object(
                *FC[:2], FC[2], machine,
                {"metadata": {"labels": {"managed-by": MANAGED}}})
            log.info("%s: peered", machine)

    def report_blocked(self, machine, said):
        """Name why a peering is hanging, WHILE it hangs.

        liqoctl's own message when it gives up is "timed out waiting for the
        condition", which reads as a fault on the rented machine. The real
        answer is already written on the gateway pod.

        What separates the two pods here is not how long they have waited but
        whether the scheduler has RULED. PodScheduled=False is final until the
        cluster itself changes, so it is acted on at once -- no threshold, no
        grace period. A pod merely Pending might still be placed: it waits
        stuck_pod_s and is only ever REPORTED. That is why "Pending too long"
        cannot be the rule for both -- by age they are identical.

        Accepted race: refused pods are re-queued with up to ~10s of backoff, so
        a verdict can be that stale, and abandoning inside that window costs one
        blocked_retry_s before succeeding.

        `said` dedupes within one handshake.
        """
        patience = self.cfg.liqo.stuck_pod_s
        verdict = None
        # min_age_s=0: a refusal is acted on whatever the pod's age, so the
        # query must not filter on age. Asking for stuck_pod_s here hides every
        # refusal until it ages into a threshold never meant to gate it.
        for p in self.hub.stuck_peering_pods(0):
            if p["machine"] != machine:
                continue
            if not p["unschedulable"] and p["age_s"] < patience:
                continue
            if p["name"] not in said:
                said.add(p["name"])
                log.error("%s: %s Pending %ds -- %s",
                          machine, p["name"], p["age_s"], p["why"])
            if p["unschedulable"]:
                verdict = p
        # No second line for the abort: the one above already carries the pod
        # and the reason, and the caller logs the refusal itself.
        if verdict is not None and self.cfg.liqo.abort_blocked_peering:
            raise PeeringBlocked(
                f"{verdict['name']} cannot be scheduled: {verdict['why']}")

    def ensure_slice(self, machine, kubeconfig, pool):
        """Advertise the machine's honest capacity. Safe to retry."""
        try:
            with as_file(kubeconfig) as path:
                self.create_slice(machine, pool, self.remote_capacity(path))
        except k8s.exceptions.ApiException as e:
            # A 403 whose cause is NamespaceTerminating is not RBAC. Left as a
            # raw ApiException it surfaces as a wall of HTTP headers under the
            # word "Forbidden", which sends the reader to the ClusterRole -- and
            # the ClusterRole is fine. Something is tearing this machine down.
            if e.status == 403 and "is being terminated" in (e.body or ""):
                raise TenantTerminating(tenant_ns(machine)) from None
            raise
        log.info("%s: resource slice created", machine)

    def ensure_node_metadata(self, machine, pool):
        """Put the pool's labels and taints on the machine's virtual node —
        without them every virtual node looks identical to the scheduler.

        Liqo creates the VirtualNode from the ResourceSlice, asynchronously, so
        this patches and keeps converging rather than running once. Verified
        against Liqo v1.0.1: forge.MutateVirtualNode MERGES Spec.Labels (so keys
        added here survive), never writes Spec.Taints, and
        liqoNodeProvider.updateFromVirtualNode applies both to the Node at
        runtime — so a removal here reaches the node too, with no vk restart.

        Diffed before writing: this runs many times a pass for every machine.

        Returns whether the node is SETTLED -- its VirtualNode exists and now
        carries the pool's metadata. False covers both "no VirtualNode yet"
        (normal for the first seconds after the slice) and "the patch failed",
        because the caller does the same thing with either: come back to it.
        """
        # POOL_LABEL is always present, separate from the operator's own
        # nodeLabels: it is what still attributes a peered machine to its pool
        # when its credentials Secret has been lost.
        want_labels = {**pool.node_labels, POOL_LABEL: pool.name}
        want_taints = [{"key": t.key, "value": t.value, "effect": t.effect}
                       for t in pool.taints]
        try:
            items = self.crd.list_namespaced_custom_object(
                *VN[:2], tenant_ns(machine), VN[2])["items"]
        except k8s.exceptions.ApiException as e:
            if e.status != 404:
                log.warning("%s: virtualnode list failed: %s", machine, e.status)
            return False

        # Liqo creates the VirtualNode from the ResourceSlice asynchronously, so
        # an empty list is the normal state for the first seconds after peering
        # -- not settled, not an error, just not yet.
        if not items:
            return False

        settled = True
        for vn in items:
            name = vn["metadata"]["name"]
            spec = vn.get("spec") or {}
            have_labels = spec.get("labels") or {}
            have_taints = spec.get("taints") or []
            # Subset, not equality: Liqo puts its own labels in the same map,
            # and demanding equality would be a write every pass.
            labels_ok = all(have_labels.get(k) == v
                            for k, v in want_labels.items())
            taints_ok = _same_taints(have_taints, want_taints)
            if labels_ok and taints_ok:
                continue
            patch = {"spec": {"labels": {**have_labels, **want_labels},
                              "taints": want_taints}}
            try:
                self.crd.patch_namespaced_custom_object(
                    *VN[:2], tenant_ns(machine), VN[2], name, patch)
            except k8s.exceptions.ApiException as e:
                log.warning("%s: virtualnode metadata patch failed: %s",
                            machine, e.status)
                settled = False
                continue
            log.info("%s: virtual node labelled for pool %s (labels=%s taints=%d)",
                     machine, pool.name, want_labels, len(want_taints))
        return settled

    def unpeer(self, machine, kubeconfig):
        """Graceful teardown. Only possible while the machine still answers.

        Deliberately NOT --delete-namespaces. That flag makes liqoctl delete the
        tenant namespace and then WAIT for it, and the wait can never finish:
        the namespace is held by crdreplicator.liqo.io/* finalizers that clear
        only by talking to the remote cluster. liqoctl burns the whole
        peer_timeout, once per machine, and then force_teardown() clears it in
        seconds by stripping those finalizers -- which liqoctl cannot do. The
        namespace is force_teardown()'s job; see its docstring.
        """
        with as_file(kubeconfig) as path:
            self.liqoctl(["unpeer",
                          "--timeout", self.cfg.liqo.peer_timeout],
                         remote_kubeconfig=path)

    # ------------------------------------------------------------- teardown --
    def unstick(self, gvr, ns, name):
        """Strip finalizers so an already-issued delete can finish.

        Call AFTER delete, never instead of it: the object then carries a
        deletionTimestamp, so dropping the finalizer completes the removal. Doing
        it the other way round leaves a live, unprotected object behind if the
        delete that was supposed to follow ever fails.

        This is force-removal of a guard another controller placed, which is only
        defensible because of who placed it: Liqo's crdreplicator finalizes these
        objects and can only clear them by talking to the remote cluster -- and
        force_teardown() runs exactly when that cluster is unreachable. There is
        no amount of waiting that resolves it.
        """
        try:
            self.crd.patch_namespaced_custom_object(
                *gvr[:2], ns, gvr[2], name, {"metadata": {"finalizers": None}})
        except k8s.exceptions.ApiException as e:
            if e.status != 404:
                log.warning("%s: finalizer strip failed: %s", name, e.status)

    def force_teardown(self, machine, expected=False):
        """Hub-side-only cleanup for a machine that is already gone.

        `expected=True` is a sweep after a successful unpeer rather than a
        rescue: same work, but it should not read like a machine died. Every
        delete tolerates 404, so the call is safe when nothing is left.

        liqoctl unpeer talks to the remote cluster, so it cannot help once the box
        is dead -- and without this the hub keeps a tenant namespace, a
        ForeignCluster, a gateway client and an Identity forever. This is the one
        failure mode the shared-K3s design did not have. Do not remove it.

        Order matters, and completeness matters more than it looks. Liqo's
        NamespaceOffloading controller refuses to configure ANY mapping when the
        VirtualNode count and the NamespaceMap count disagree
        (clusterselector.go: "number of foreign clusters (%d) does not match that
        of NamespaceMaps (%d)"). One leftover VirtualNode from a half-built
        peering therefore stops offloading for EVERY cluster, and the symptom
        shows up on an unrelated healthy machine as pods stuck in
        PodOffloadingBackOff. Delete the namespaced objects explicitly rather
        than trusting the namespace deletion to cascade in time.

        The ResourceSlice and the NamespaceMap both carry a
        `crdreplicator.liqo.io/*` finalizer, and the crdreplicator clears it by
        talking to the remote cluster. So the deletes are accepted and then
        block forever: the tenant namespace sits Terminating, its NamespaceMap
        stays alive and counted, the counts stay out of step, and the NEXT
        machine to peer comes up healthy but with offloading disabled. Deleting
        the namespace does not help either; it waits on the same finalizers.
        Every object we cannot otherwise remove has to be unstuck explicitly.
        """
        if expected:
            log.info("%s: hub-side sweep after unpeer", machine)
        else:
            log.warning("%s: force teardown (remote unreachable)", machine)
        ns = tenant_ns(machine)

        # ResourceSlice first: deleting it is what retracts the advertised capacity.
        try:
            self.crd.delete_namespaced_custom_object(*RS[:2], ns, RS[2], machine)
        except k8s.exceptions.ApiException as e:
            if e.status != 404:
                log.warning("%s: resourceslice delete failed: %s", machine, e.status)
        self.unstick(RS, ns, machine)

        # Then the VirtualNode, which owns the Node object and the NamespaceMap.
        try:
            for vn in self.crd.list_namespaced_custom_object(
                    *VN[:2], ns, VN[2])["items"]:
                self.crd.delete_namespaced_custom_object(
                    *VN[:2], ns, VN[2], vn["metadata"]["name"])
        except k8s.exceptions.ApiException as e:
            if e.status != 404:
                log.warning("%s: virtualnode delete failed: %s", machine, e.status)

        # The NamespaceMap explicitly, rather than as a cascade from its owning
        # VirtualNode: ownership does not survive a finalizer that cannot run,
        # and this object is the one whose survival breaks offloading
        # cluster-wide.
        try:
            for nm in self.crd.list_namespaced_custom_object(
                    *NM[:2], ns, NM[2])["items"]:
                name = nm["metadata"]["name"]
                try:
                    self.crd.delete_namespaced_custom_object(
                        *NM[:2], ns, NM[2], name)
                except k8s.exceptions.ApiException as e:
                    if e.status != 404:
                        log.warning("%s: namespacemap delete failed: %s",
                                    machine, e.status)
                self.unstick(NM, ns, name)
        except k8s.exceptions.ApiException as e:
            if e.status != 404:
                log.warning("%s: namespacemap list failed: %s", machine, e.status)

        # The Node object is cluster-scoped and outlives its tenant namespace if
        # the VirtualNode finalizer could not run.
        self.hub.delete_node(machine)

        try:
            self.crd.delete_cluster_custom_object(*FC[:2], FC[2], machine)
        except k8s.exceptions.ApiException as e:
            if e.status != 404:
                log.warning("%s: foreigncluster delete failed: %s", machine, e.status)
        try:
            self.core.delete_namespace(ns)
        except k8s.exceptions.ApiException as e:
            if e.status != 404:
                log.warning("%s: tenant namespace delete failed: %s",
                            machine, e.status)

        # Whatever the namespace is still waiting on. The peering credentials
        # (kubeconfig-controlplane-*) are finalized by the crdreplicator too, and
        # one held Secret keeps the namespace Terminating just as effectively as
        # a held NamespaceMap -- which then keeps the tenant namespace name
        # taken, so a machine that ever reuses this name cannot be peered again.
        try:
            for s in self.core.list_namespaced_secret(ns).items:
                if s.metadata.finalizers:
                    self.core.patch_namespaced_secret(
                        s.metadata.name, ns, {"metadata": {"finalizers": None}})
        except k8s.exceptions.ApiException as e:
            if e.status != 404:
                log.warning("%s: tenant secret cleanup failed: %s",
                            machine, e.status)
