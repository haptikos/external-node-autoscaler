"""Everything the operator reads from, or writes to, its own cluster.

The hub is the single source of truth for supply. Virtual nodes ARE the workers,
so demand, capacity and utilisation are all local queries and no remote
kubeconfig is needed for accounting — only for the peering handshake itself.

Fails when the Kubernetes API misbehaves. Peering lives in liqo.py, decisions in
controller.py.
"""
import base64
import json
import os
import time

from kubernetes import client as k8s

from . import logging_setup, pki, pools, resources
from .labels import (FLAVOR_LABEL, MACHINE_LABEL, MANAGED, POOL_LABEL,
                     REMOTE_CLUSTER_ID_LABEL, TENANT_NS_SELECTOR,
                     VNODE_LABEL)

log = logging_setup.get("hub")

__all__ = ["Hub", "MACHINE_LABEL", "MANAGED", "POOL_LABEL", "VNODE_LABEL",
           "SA_DIR", "parse_cpu", "parse_mem_gi"]

SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"


def parse_cpu(v):        # "13" | "1500m" -> float cores
    return float(v[:-1]) / 1000 if str(v).endswith("m") else float(v)


def parse_mem_gi(v):     # "40Gi" | "40960Mi" | "42949672960" -> float GiB
    s = str(v)
    for suf, mul in (("Ki", 1 / 1024 ** 2), ("Mi", 1 / 1024), ("Gi", 1),
                     ("Ti", 1024)):
        if s.endswith(suf):
            return float(s[:-2]) * mul
    return float(s) / 1024 ** 3


class Hub:
    def __init__(self, core, crd, cfg):
        self.core = core
        self.crd = crd
        self.cfg = cfg
        self.ns = cfg.hub.namespace

    # ------------------------------------------------------------ identity --
    def secret_name(self, machine):
        return f"k3s-{machine}"

    def kubeconfig_path(self):
        """liqoctl loads kubeconfigs the kubectl way and will not pick up
        in-cluster credentials on its own, so materialise one from the projected
        service-account token."""
        path = "/tmp/hub.kubeconfig"
        with open(f"{SA_DIR}/token", encoding="utf-8") as fh:
            token = fh.read().strip()
        with open(f"{SA_DIR}/ca.crt", "rb") as fh:
            ca = base64.b64encode(fh.read()).decode()
        host = os.environ["KUBERNETES_SERVICE_HOST"]
        port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
        cfg = {
            "apiVersion": "v1", "kind": "Config", "current-context": "hub",
            "clusters": [{"name": "hub", "cluster": {
                "server": f"https://{host}:{port}",
                "certificate-authority-data": ca}}],
            "users": [{"name": "hub", "user": {"token": token}}],
            "contexts": [{"name": "hub",
                          "context": {"cluster": "hub", "user": "hub"}}],
        }
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh)
        return path

    # -------------------------------------------------------------- demand --
    def pending_demand(self):
        """{pool name -> [(pod ref, Resources)]}, plus the pods."""
        demand = {p.name: [] for p in self.cfg.pools}
        unroutable = []
        for ns in self.cfg.watch_namespaces:
            for p in self.core.list_namespaced_pod(ns).items:
                if p.status.phase != "Pending":
                    continue
                unsched = any(c.type == "PodScheduled" and c.status == "False"
                              and c.reason == "Unschedulable"
                              for c in (p.status.conditions or []))
                if not unsched:
                    continue
                pool = pools.match(p, self.cfg.pools)
                if pool is None:
                    unroutable.append(p)
                    continue
                ref = f"{p.metadata.namespace}/{p.metadata.name}"
                demand[pool.name].append((ref, resources.pod_requests(p)))
        if unroutable:
            log.warning(
                "%d pending pod(s) match no pool and will not be scheduled: "
                "%s. Check the pod's nodeSelector/affinity and tolerations "
                "against the declared pools (%s).",
                len(unroutable),
                ", ".join(f"{p.metadata.namespace}/{p.metadata.name}"
                          for p in unroutable[:5])
                + (" ..." if len(unroutable) > 5 else ""),
                ", ".join(p.name for p in self.cfg.pools))
        return demand, unroutable

    # -------------------------------------------------------------- supply --
    def virtual_nodes(self):
        """Our virtual nodes. One per rented machine, named after it.

        `capacity` is the node's whole allocatable vector.
        """
        out = {}
        for n in self.core.list_node(label_selector=VNODE_LABEL).items:
            name = n.metadata.name
            if not name.startswith(self.cfg.name_prefix):
                continue
            ready = any(c.type == "Ready" and c.status == "True"
                        for c in (n.status.conditions or []))
            transition = max((c.last_transition_time for c in n.status.conditions
                              if c.type == "Ready"), default=None)
            node_labels = n.metadata.labels or {}
            out[name] = {
                "ready": ready,
                # Falling back to "now" rather than 0.
                "since": (transition.timestamp() if transition else time.time()),
                "capacity": resources.Resources.from_api(
                    n.status.allocatable or {}),
                # Stamped by liqo.ensure_node_metadata: the fallback source of
                # a machine's pool when its Secret is gone.
                "pool": node_labels.get(POOL_LABEL),
            }
        return out

    def stuck_peering_pods(self, min_age_s):
        """Liqo pods a peering created on the hub that the scheduler cannot
        place, each with the scheduler's own verdict.

        NAMESPACE BY NAMESPACE, not one cluster-wide pod list. `namespace` is
        part of the storage key prefix, so a namespaced list reads one small
        range; a cluster-wide list materialises every pod and filters after, and
        no field selector fixes that (`status.phase` is not indexed).

        Enumerated from Liqo's own label, not from machines we know about: a
        tenant namespace routinely outlives its machine -- that is what a leaked
        peering looks like.
        """
        now = time.time()
        out = []
        for ns_obj in self.core.list_namespace(
                label_selector=TENANT_NS_SELECTOR).items:
            ns = ns_obj.metadata.name
            machine = (ns_obj.metadata.labels or {}).get(REMOTE_CLUSTER_ID_LABEL)
            if not machine:
                log.warning("tenant namespace %s carries no %s -- cannot say "
                            "whose peering it is, so its pods go unchecked",
                            ns, REMOTE_CLUSTER_ID_LABEL)
                continue
            if not machine.startswith(self.cfg.name_prefix):
                continue
            for p in self._tenant_pods(ns):
                out.extend(self._stuck(p, ns, machine, now, min_age_s))
        return out

    def _tenant_pods(self, ns):
        """Pods in one tenant namespace, tolerating the namespace vanishing.

        404 only -- the namespace was deleted between the two calls, so "no pods
        here" is the true answer. Anything else propagates: the caller releases
        peering backoffs on an empty result, so an incomplete read must never be
        reported as an empty one.
        """
        try:
            return self.core.list_namespaced_pod(ns).items
        except k8s.exceptions.ApiException as e:
            if e.status == 404:
                return []
            raise

    def _stuck(self, p, ns, machine, now, min_age_s):
        """One pod's entry, or nothing."""
        if p.status.phase != "Pending":
            return []
        created = p.metadata.creation_timestamp
        # A pod with no timestamp is brand new as far as we can tell, which is
        # the reading that does NOT produce a false alarm.
        age = now - created.timestamp() if created else 0.0
        if age < min_age_s:
            return []
        cond = next((c for c in (p.status.conditions or [])
                     if c.type == "PodScheduled" and c.status == "False"), None)
        return [{
            "name": p.metadata.name,
            "namespace": ns,
            "machine": machine,
            "age_s": int(age),
            # PodScheduled=False is the scheduler saying it TRIED and could
            # not; its absence means it has not ruled yet. The message is passed
            # through verbatim -- it names every constraint that failed, which
            # is more than this operator could reconstruct.
            "unschedulable": cond is not None,
            "why": getattr(cond, "message", None) or "no scheduling condition"
                   " reported yet",
        }]

    def used_on(self, vnode):
        """What non-daemonset pods hold on this virtual node, as a vector.

        Deliberately NOT restricted to Running. A pod holds capacity from the
        moment the scheduler binds it, and it can legitimately sit Pending
        there for a long while.

        Pods already terminating are excluded: they will not start, and a
        zombie Terminating pod would otherwise pin a rented machine alive.
        """
        total = resources.Resources()
        pods_on = self.core.list_pod_for_all_namespaces(
            field_selector=f"spec.nodeName={vnode}").items
        for p in pods_on:
            if p.status.phase in ("Succeeded", "Failed"):
                continue
            if p.metadata.deletion_timestamp is not None:
                continue
            if any(o.kind == "DaemonSet"
                   for o in (p.metadata.owner_references or [])):
                continue
            total = total + resources.pod_requests(p)
        return total

    # --------------------------------------------------------- credentials --
    def write_credentials(self, machine, creds, pool=None):
        """Persist what the operator keeps for one machine.

        Written BEFORE the machine is created — see Controller.provision. No
        kubeconfig is stored: the machine has no address yet.

        The pool goes on as a label; see POOL_LABEL for why the Secret and not
        the machine name carries it.
        """
        labels = {"managed-by": MANAGED, MACHINE_LABEL: machine}
        if pool:
            labels[POOL_LABEL] = pool
        body = k8s.V1Secret(
            metadata=k8s.V1ObjectMeta(
                name=self.secret_name(machine), labels=labels),
            data={
                "client.crt": base64.b64encode(creds.client_crt).decode(),
                "client.key": base64.b64encode(creds.client_key).decode(),
                "server-ca.crt": base64.b64encode(
                    creds.server_ca_crt).decode(),
            })
        try:
            self.core.create_namespaced_secret(self.ns, body)
        except k8s.exceptions.ApiException as e:
            # A restart mid-provision can retry a name; whichever machine held
            # the old credentials is unreachable anyway.
            if e.status != 409:
                raise
            self.core.replace_namespaced_secret(
                self.secret_name(machine), self.ns, body)

    def machine_credentials(self):
        """machine -> Credentials. Not kubeconfigs: those need an address, which
        comes from the provider inventory and is re-read every pass."""
        out = {}
        for s in self.core.list_namespaced_secret(
                self.ns, label_selector=f"managed-by={MANAGED}").items:
            machine = (s.metadata.labels or {}).get(MACHINE_LABEL)
            data = s.data or {}
            if not machine or not {"client.crt", "client.key",
                                   "server-ca.crt"} <= set(data):
                continue
            stamp = s.metadata.creation_timestamp
            out[machine] = pki.Credentials(
                client_crt=base64.b64decode(data["client.crt"]),
                client_key=base64.b64decode(data["client.key"]),
                server_ca_crt=base64.b64decode(data["server-ca.crt"]),
                # The API server's clock, so the orphan rule survives a
                # restart. See Credentials.created.
                created=stamp.timestamp() if stamp else None,
                pool=(s.metadata.labels or {}).get(POOL_LABEL),
                flavor=(s.metadata.labels or {}).get(FLAVOR_LABEL))
        return out

    def set_flavor(self, machine, flavor):
        """Record which flavor the provider actually gave us.

        Patched after create() rather than written with the credentials,
        because the credentials must exist BEFORE the machine is ordered and
        the flavor is only known afterwards.
        """
        try:
            self.core.patch_namespaced_secret(
                self.secret_name(machine), self.ns,
                {"metadata": {"labels": {FLAVOR_LABEL: flavor}}})
        except k8s.exceptions.ApiException as e:
            if e.status != 404:
                log.warning("%s: could not record flavor %s: %s",
                            machine, flavor, e.status)

    def set_endpoint(self, machine, endpoint):
        """Observability only, never read back — the kubeconfig is always derived
        from the live provider inventory, so a stale value cannot mislead the
        loop."""
        try:
            self.core.patch_namespaced_secret(
                self.secret_name(machine), self.ns,
                {"data": {"endpoint": base64.b64encode(
                    endpoint.encode()).decode()}})
        except k8s.exceptions.ApiException as e:
            if e.status != 404:
                raise

    def drop_secret(self, machine):
        try:
            self.core.delete_namespaced_secret(self.secret_name(machine), self.ns)
        except k8s.exceptions.ApiException as e:
            if e.status != 404:
                raise

    # ----------------------------------------------------------- condemned --
    def condemned(self):
        """Machines an operator has explicitly asked us to recycle right now.

        A machine whose cloud-init wedged otherwise has to wait out the boot
        timeout before it is reaped. That is the correct default for an
        unattended loop and unbearable while iterating on provision.sh, so
        `make recycle MACHINE=...` writes the name here and the next pass acts
        on it regardless of any timer.
        """
        try:
            cm = self.core.read_namespaced_config_map(
                self.cfg.hub.condemned_configmap, self.ns)
        except k8s.exceptions.ApiException as e:
            if e.status == 404:
                return set()
            raise
        return set((cm.data or {}).keys())

    def unmark_condemned(self, machine):
        """Drop the key so a recycled name can be reused without being re-reaped."""
        try:
            self.core.patch_namespaced_config_map(
                self.cfg.hub.condemned_configmap, self.ns,
                {"data": {machine: None}})
        except k8s.exceptions.ApiException as e:
            if e.status != 404:
                log.warning("%s: could not clear condemned marker: %s",
                            machine, e.status)

    # --------------------------------------------------------------- nodes --
    def delete_node(self, name):
        try:
            self.core.delete_node(name)
        except k8s.exceptions.ApiException as e:
            if e.status != 404:
                log.warning("%s: node delete failed: %s", name, e.status)
