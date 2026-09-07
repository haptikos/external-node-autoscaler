"""Wire the pieces together and run the loop.

This is the only module that constructs anything global — API clients, the
provider, the controller. Everything else receives what it needs, which is what
makes the reconcile rules testable without a cluster: before this split,
importing the operator built a Kubernetes client and an OVH client at module
scope, so no part of it could run without both sets of credentials.
"""
import os
import time

from kubernetes import client as k8s, config as k8s_config

from . import logging_setup, providers
from .config import Config
from .controller import Controller
from .hub import Hub, SA_DIR
from .liqo import Liqo
from .pools import UNLIMITED


def _limit(n):
    """0 is unlimited, and a bare "max=0" reads as "rent nothing"."""
    return "unlimited" if n == UNLIMITED else str(n)


def main():
    cfg = Config.load()
    logging_setup.setup(cfg.log_level)
    log = logging_setup.get("main")

    if os.path.exists(f"{SA_DIR}/token"):
        k8s_config.load_incluster_config()
    else:
        k8s_config.load_kube_config()

    hub = Hub(k8s.CoreV1Api(), k8s.CustomObjectsApi(), cfg)
    liqo = Liqo(hub, cfg)
    loaded = providers.load_all(cfg.providers, cfg.pools)
    shapes = providers.flavor_shapes(loaded, cfg.pools)
    ctrl = Controller(hub, liqo, loaded, cfg, flavor_shapes=shapes)

    log.info("up: fleet max=%s idle=%ds dead=%ds liqo=%s "
             "(one k3s cluster per machine)",
             _limit(cfg.scale.max_machines), cfg.scale.idle_s,
             cfg.scale.dead_s, cfg.liqo.version)
    for pool in cfg.pools:
        log.info("pool %s: min=%d max=%s reserve=%s providers=%s labels=%s "
                 "taints=%d",
                 pool.name, pool.min_machines, _limit(pool.max_machines),
                 pool.reserve(),
                 ",".join(f"{e.name}({e.priority})" for e in pool.providers),
                 pool.node_labels or "{}", len(pool.taints))
    ctrl.log_flavors()
    log.info("providers available: %s", providers.available())
    if cfg.hub_egress_ips:
        log.info("rented machines will allow the hub only from: %s",
                 " ".join(cfg.hub_egress_ips))
    else:
        log.warning("no hub egress allowlist — rented machines will accept the "
                    "k3s API and WireGuard from ANY source")

    # Both fail fast on purpose. A bad provider credential or a liqoctl version
    # skew otherwise surfaces minutes later, inside a peering, on a machine that
    # is already being billed for.
    liqo.start()
    liqo.check_version()
    for provider in loaded.values():
        provider.validate()

    while True:
        try:
            ctrl.reconcile()
        except Exception:                                       # noqa: BLE001
            # exception(), not str(e): the traceback is the only thing that says
            # WHICH call failed, and losing it was the single biggest reason a
            # reconcile failure was hard to diagnose from `kubectl logs`.
            log.exception("reconcile failed")
        time.sleep(cfg.interval)


if __name__ == "__main__":
    main()
