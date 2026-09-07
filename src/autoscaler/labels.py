"""Label keys and values that cross a tree boundary.

These are the identifiers the cloud-init template in the Helm chart writes and
the operator reads back — the only naming contract in this system whose two
halves live in different directories and are deployed by different commands.
Rename one side alone and a machine boots, marks itself ready with a label
nobody selects, never becomes a virtual node, and is terminated by the boot
timeout with nothing in the log about labels.
"""

#: Value of the `managed-by` label. Also the selector for every list call that
#: distinguishes machines this operator owns from the hand-managed cluster.
MANAGED = "external-node-autoscaler"

#: Label key holding the machine name on the Secret the operator writes.
MACHINE_LABEL = "external-node-autoscaler/machine"

#: The pool a machine was rented for. On the Secret rather than in the machine
#: name because the Secret is written BEFORE the machine is ordered, so it is
#: the only record that exists while the machine is still booting — and a rename
#: would strand every running machine.
POOL_LABEL = "external-node-autoscaler/pool"

#: The flavor a machine was rented as, patched onto its Secret once the
#: provider says which one it got. A machine still booting has no node to
#: measure, and sizing it as the pool's FIRST flavor when a bigger one was
#: rented for a big pod means the pod never fits the assumed free space and the
#: pool rents another machine every pass.
FLAVOR_LABEL = "external-node-autoscaler/flavor"

#: THE READINESS SIGNAL, stamped on the machine's own Node as the last line of
#: its provisioning script. One label rather than a list of remote checks
#: because only the script knows what "finished" means — k3s serving, GPU
#: allocatable, device plugin patched, Liqo installed, ufw up.
BOOTSTRAP_LABEL = "external-node-autoscaler/bootstrap"
BOOTSTRAP_DONE = "complete"

#: How Liqo marks the nodes it projects. Not ours, not renameable.
VNODE_LABEL = "liqo.io/type=virtual-node"

#: Liqo derives one namespace per peering from the provider cluster id, which is
#: the machine name. Everything a peering runs ON THE HUB lives there: the
#: `gw-<machine>` gateway and the `vk-<machine>` virtual kubelet.
#:
#: Used to BUILD names, never to read them back -- the labels below are Liqo
#: saying which namespaces are tenants and whose they are, where a name is only
#: an inference.
TENANT_NS_PREFIX = "liqo-tenant-"

#: Liqo's own mark on a tenant namespace, as opposed to a name anyone could
#: create by hand.
TENANT_NS_SELECTOR = "liqo.io/tenant-namespace=true"

#: The machine a tenant namespace belongs to, stated by Liqo. Stamped on every
#: tenant namespace it creates, so one without it is not "old", it is
#: unexplained -- hub.py skips it and says so rather than inferring.
REMOTE_CLUSTER_ID_LABEL = "liqo.io/remote-cluster-id"
