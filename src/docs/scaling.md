# How scaling decides what to rent

Demand and capacity are **vectors** over `cpu`, `memory`, `pods` and any
extended resource a pod asks for. There is no single "scale resource" and no
`unitsPerMachine`: a pod that wants 6 CPU, 20Gi and 1 GPU is measured as exactly
that.

## The pass

1. **Demand.** Every Unschedulable pending pod that matches a pool
   (`pools.match`) is sized by `resources.pod_requests` — Kubernetes' effective
   request, `max(sum(containers), max(initContainers)) + overhead`, plus one
   `pods`.
2. **Supply.** Each Ready virtual node's free space is its allocatable minus
   what non-daemonset pods hold (`hub.used_on`). Per node, so a pool holding
   machines of different sizes is measured machine by machine.
3. **Packing.** `packing.plan` places pods into existing free space first, then
   opens bins of one machine's schedulable shape. The bin count is what gets
   rented.

Summing demand and dividing by machine size is the obvious approach and it is
wrong for pods that cannot be split: four pods wanting 3 CPU total 12, which
looks like three 4-CPU machines, but only one fits per machine and the answer is
four.

## Where a machine's shape comes from

Most-trusted first:

| source | when |
| --- | --- |
| the Ready machines already in the pool | whenever there are any |
| `Provider.flavor_capacity` + hub pod capacity | cold start |
| nothing — rent one and measure it | neither available |

A pool's flavors are tried **in declared order and the first that holds the pod
wins**, so `[d2-4, d2-8]` serves a 64Mi pod with a `d2-4` and a 4Gi pod with a
`d2-8`. A pod that fits no flavor is impossible, not merely awkward, and is
reported rather than rented for. When renting, the core passes the provider only
the flavors big enough for that machine's pods, so a stock shortage falls back
*up* and never to a machine too small to run them.

Machines opened purely to satisfy `minMachines` have no pods to size them, so
they take the pool's first flavor.

The flavor a machine was actually rented as is recorded on its Secret
(`FLAVOR_LABEL`). A machine still booting has no node to measure, and sizing it
as the pool's first flavor when a bigger one was rented for a big pod leaves
free space the pod does not fit — so the pool rents another every pass.

A dimension a provider cannot report must be **omitted, never zeroed**. Absent
means unmeasured, and the loop rents one to find out; zero would make every pod
asking for it permanently unschedulable. OVH's flavor API reports cpu and memory
but not GPU count, so that comes from `_GPU_PER_FLAVOR` in the provider source.

Pod capacity is the hub's policy (`maxPodsPerMachine`), not the flavor's, so the
controller fills it in. A shape without it goes to `pods: -1` after one pod and
hands every pod its own machine.

## Reserves

`schedulable = capacity - systemReserve - daemonsetReserve`, per pool.
`systemReserve` covers the k3s server and Liqo's control plane; `daemonsetReserve`
covers the per-node pods that land on every machine. Both default to non-zero:
a pool that declares nothing still leaves room, because advertising the whole
machine starves k3s and the node goes NotReady under load — which reads as a
dead machine rather than an over-committed one.

## Idle

A machine is idle when `used_on` is zero, and `used_on` **excludes pods owned by
a DaemonSet**. `charts/liqo-pod-reaper` fences daemonsets off virtual nodes, but
only the ones it is told about and only every five minutes; one that slips
through would otherwise hold the machine alive forever.

## Deliberately not done

Karpenter-style consolidation. Choosing which flavor best fits a pending pod —
the flavor list is a priority order, not a search space. Bin-packing optimality:
first-fit-decreasing is an approximation, and the loop re-evaluates every pass.
