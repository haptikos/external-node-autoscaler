# external-node-autoscaler

Rent a machine from any provider the moment your pods need one, attach it to a
managed Kubernetes cluster as an ordinary node — **with no per-node fee** — and
give it back when it goes idle.

The operator watches Pending pods, works out what would actually fit them, rents
one from whichever provider has stock, and releases it once nothing is using it.
The machine joins through [Liqo](https://github.com/liqotech/liqo) rather than as
a cluster member, so the hub does not bill you per attached node the way AWS EKS
Hybrid Nodes does. You pay the machine's provider, and the control plane you were
already paying for.

> **This is a proof of concept.** It works end to end with two real providers —
> OVH and Vultr — and has been run repeatedly against a live EKS cluster. It is
> published to show the mechanism, not as a supported product, yet.


## Demo

A deployment is scaled from 0 to 1 and back again, and the autoscaler rents a
machine around it and gives it back. Nothing below is stepped through by hand —
the only human action is changing the replica count. The long waits — renting
and booting the machine, and waiting out the idle timer — are cut from the
recording; everything else runs in real time.

### 1. A Pending pod becomes a rented machine

`cpu-demand` is scaled to 1. Every node in the hub is wrong for
it, so the pod sits **Pending** — which is the demand signal. The autoscaler
measures what the pod actually asked for, picks a flavor big enough, and rents a
box from Vultr. It boots, installs k3s, peers over Liqo, and joins the cluster
as `external-vultr-7d50`, a node that did not exist a minute earlier. The pod
schedules onto it.

<img width="1200" height="675" alt="A Pending pod triggers a rented machine that joins the cluster as a node" src="https://github.com/user-attachments/assets/8567a2f4-ee20-4a05-9344-bb35eaf8c42e" />

<details>
<summary><b>2. Logs and a shell, as if the pod were local</b></summary>

The container is running on a box in another cloud, and nothing about working
with it says so. `kubectl logs` streams from it, and a shell drops straight into
the container — both from the hub, through the same commands you would use for a
pod on a local node.

<img width="1200" height="675" alt="Streaming logs and opening a shell into the remote pod from the hub" src="https://github.com/user-attachments/assets/6f100552-c23f-4e20-8f7f-dc1982e2add1" />

</details>

<details>
<summary><b>3. A GPU pod serving real traffic from the rented box</b></summary>

The same loop with a GPU pool: the pod is vLLM, the rented machine is an OVH A10
box, and `provision.sh` installs the NVIDIA driver, container toolkit and device
plugin before the node ever reports `Ready`. Once the model is loaded, a request
goes in through the cluster's AWS load balancer and comes back answered — served
by a GPU in another cloud, through the Liqo tunnel, with nothing in the request
path aware of it.

<img width="800" height="500" alt="A chat completion request served by vLLM running on the rented GPU machine" src="https://github.com/user-attachments/assets/4ff8d095-b316-44e0-aa89-73095b0d84ed" />

</details>

<details>
<summary><b>4. Idle, and the machine goes back</b></summary>

The deployment is scaled to 0. The node is not released immediately — the
autoscaler waits for it to stay idle, and only a pod that is not a DaemonSet
counts as work. When the timer runs out it tears the peering down, deletes the
machine at the provider, and the node disappears from the cluster.

<img width="1200" height="675" alt="The idle machine is released and the node disappears from the cluster" src="https://github.com/user-attachments/assets/d7f856ff-1255-4fe7-be75-d40cfafd29cf" />

</details>


## 1. Autoscaling across providers

The operator in `src` runs a level-based reconcile loop: it
derives all state fresh every pass, so there is nothing to get out of sync.

Demand is **what the pods actually ask for** — cpu, memory, GPUs, pod count —
not a replica count. Pending, unschedulable pods are bin-packed against the free
space on existing nodes; whatever does not fit becomes the machines to rent.

Choosing a machine walks the configuration in order:

```yaml
pools:
  - name: gpu
    providers:
      - name: ovh
        priority: 10
        flavors: [a10-45, rtx5000-28]
      - name: vultr
        priority: 20
        flavors: [vcg-a40-24c-120g-48vram]
```

For each pod, every flavor is checked **one by one**: is it big enough, is it
sold in this region, is it in stock right now. The first that answers gets
rented. When a provider has nothing, the pool falls through to the next one in
the same pass rather than stalling — which is the point of having two.

Machines are released when they go idle, and only DaemonSets running on a node
do not count as "busy".


## 2. Bring your own node

Renting on demand pays off when the machine is worth renting: big enough to
matter, and cheaper than the equivalent instance in the hub's own cloud — which
is most often true of GPUs. Sometimes it is not about price at all, but about
which provider actually has stock. Either way the saving only survives if
attaching the machine does not add a bill of its own, and that is what this part
is for.

AWS has a built-in answer to this, EKS Hybrid Nodes, and it bills you **per vCPU
per hour** for every attached machine, on top of what the machine already costs
you at its own provider.

This does not. The rented machine runs its own single-node k3s cluster and is
attached through [Liqo](https://github.com/liqotech/liqo) as a *virtual node*. To the hub cluster
that is an ordinary node object; to AWS it is not a node at all, so there is no
per-vCPU charge. You pay the machine's provider, and the EKS control plane you
were already paying for.

The same mechanism is not AWS-specific. Liqo peers Kubernetes to Kubernetes, so
a GKE or AKS hub would work the same way. Only two things here are EKS-shaped:
the terraform under `experiment/`, and
[`liqo-values/values-eks.yaml`](liqo-values/values-eks.yaml) — which carries the
NLB annotation for the Liqo gateway, `fullMasquerade` for how EKS nodeports
rewrite source addresses, and the AWS certificate signer for the virtual
kubelet.

### What it looks like to a developer

Nothing unusual, which is the point:

- A pod gets **scheduled onto a node**. No new CRD to learn, no separate
  runtime, no sidecar. `nodeSelector` and tolerations pick the pool.
- `kubectl logs`, `kubectl describe pod`, `kubectl exec` all work as normal
  against the pod, even though the container is running on a box in another
  cloud.
- `kubectl get nodes` shows the machine, and `kubectl describe node` shows its
  capacity, allocatable and the pods bound to it.
- The node and its pods appear in the **AWS console's cluster view**, including
  node load, like any other node in the cluster.

AWS agrees. The rented box shows up under **Compute → Nodes** on the cluster,
and opening it gives the same detail page as an EC2-backed node: capacity
allocation, the pods bound to it, conditions and events. `Instance type` is
empty and `Compute` reads *Self-managed*, which is the only hint that the
machine is not AWS's.

<img width="1200" height="592" alt="The rented node in the AWS console: node list, capacity allocation, its pods, conditions and events" src="https://github.com/user-attachments/assets/4629668b-e374-431d-8458-f94d4b2f9f4a" />

### Getting traffic to the pod

One thing does not come for free: a load balancer cannot be pointed at the
offloaded pod directly. In `ip` target mode an ALB registers **pod** addresses,
and this pod's address belongs to the *remote* cluster's CIDR — reachable only
inside the Liqo tunnel, which is not a route an AWS appliance has. `instance`
mode does work, at the cost of a NodePort and controller-managed rules on the
node security group.

`experiment/` takes a third option: a small nginx pod on a hub node, in a
namespace that is deliberately **not** offloaded. It has an ordinary VPC address
the ALB can target, and it reaches the service the way any in-cluster client
would. No NodePort anywhere. See
[`experiment/charts/inference/templates/gateway.yaml`](experiment/charts/inference/templates/gateway.yaml).

### Storage

A pod can mount a **hostPath on the rented machine** — that is how the model
cache works, with `provision.sh` creating the directory and the deployment
mounting it. That is enough to keep a large download off the critical path while
a machine lives, but it lives and dies with the machine: a newly rented box
always starts cold.

Creating a real volume at the provider and attaching it to the machine would fix
that, and would let PersistentVolumeClaims work against remote nodes. Neither is
implemented — today, treat a rented machine as scratch.

## What a provider has to offer

The rented box runs **its own single-node k3s server**, so it has to be a real
virtual machine you get root on — not a managed container.

- **Root on a real VM.** `provision.sh` installs k3s from `get.k3s.io`, loads
  kernel modules (`modprobe nvidia` on GPU boxes) and writes iptables rules for
  the pod CIDR. None of that is possible where you are handed a container.
- **A routable public IPv4.** The hub opens the peering to the machine; the
  machine never calls in.
- **An API** to create, list and delete instances, and to see flavors and stock.

Two more that the current implementation leans on, neither of them fundamental:

- **cloud-init user data**, which is how the box receives its certificate
  authorities and the provisioning script today. A provider without it could be
  handled by connecting to the machine once it is up and pushing the same
  payload — that is simply not implemented.
- **Control of its own firewall.** The machine runs `ufw`, denying everything
  inbound except SSH and the ports the hub needs. Drop it where the provider
  gives you security groups instead, or where you would rather manage it
  elsewhere; nothing else depends on it.

The root requirement is the one that rules out the GPU container marketplaces. RunPod, for example,
hands you a container on someone else's kernel rather than a machine of your
own, so k3s cannot run and the box can never join. The providers that work here
are the ones renting plain VMs.

## Why only two providers

Adding a provider is a small, well-bounded job — a directory with four methods
in it, no change to the core, the chart or the Dockerfile. With an LLM it is a
few hours of work.

What is *not* cheap is getting an account with GPU capacity actually approved
and enough credit to test against. That, not the code, is why there are two.

See [`src/README.md`](src/README.md) for
what a provider has to implement.

## Layout

```
src/   the operator: reconcile loop, packing, providers
charts/                    its Helm chart — pools, flavors, reserves, limits
liqo-values/               Liqo values for the hub
experiment/                terraform + Makefile to try it on a fresh AWS account
```

`src`, `charts` and `liqo-values` are the solution. `experiment` is the harness
that stands up a cluster to run it on — see
[`experiment/README.md`](experiment/README.md) to get started.

## How a machine joins

The hub never trusts a machine it did not create, and the machine never calls
the hub:

1. The operator generates that machine's certificate authorities up front and
   keeps them in a Secret.
2. It rents a box, handing it those CAs as cloud-init user data.
3. The box installs k3s and Liqo, opens only the ports the hub needs, and labels
   itself complete.
4. The operator peers with it, and the ResourceSlice Liqo returns becomes a
   virtual node carrying the pool's labels and taints.

Because the CAs are minted first, the operator can assemble a kubeconfig for a
cluster that has not booted yet, and a machine that never boots leaves nothing
behind but a Secret that gets reaped.

## Status and limits

What is not there:

- No consolidation or rebalancing. Packing is first-fit-decreasing, an
  approximation on purpose.
- Nothing here is production-hardened: no HA for the operator, no alerting, no
  cost controls beyond a machine ceiling per pool and per fleet.
- Two providers, OVH and Vultr. Both work; neither is exhaustively exercised.
- Liqo is pinned to v1.2.0 and must match on both sides.

## License

MIT — see [LICENSE](LICENSE). That covers the operator, the chart and the
terraform; it is all original work.

[Liqo](https://github.com/liqotech/liqo) is Apache-2.0 and is used as a
product, not as source: nothing here vendors or modifies it. The one place that
matters is the operator image, which bakes in the `liqoctl` binary and so
redistributes it — the image therefore carries Liqo's license at
`/licenses/liqo/LICENSE`. Everything else — the Liqo chart on the hub, and the
`liqoctl` each rented machine installs from cloud-init — is downloaded from
upstream at install time, onto your own infrastructure.
