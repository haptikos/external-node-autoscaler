"""Demand-driven, level-based autoscaler for the EKS+Liqo bridge.

Each rented machine is its OWN single-node K3s cluster, peered with the hub in
its own right. One cluster -> one node -> one virtual node: the 1:1 mapping
between virtual and real capacity is a property of the topology, not something
patched on afterwards with an offloadingPatch nodeSelector.

Where things live:

    config.py       every knob, validated at startup
    hub.py          Kubernetes accounting — demand, supply, secrets
    liqo.py         peering, resource slices, teardown
    controller.py   the eight reconcile rules
    providers/      where machines come from; one directory each

The hub is the single source of truth for supply. Virtual nodes ARE the workers,
so no second kubeconfig is needed for accounting — only for the peering
handshake itself. Every scale event and every error is logged before it is acted
on, so `kubectl logs` is the whole audit trail; there is no push alerting.
"""
