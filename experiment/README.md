# experiment

Everything needed to try the solution on a **brand-new AWS account** — no
pre-existing VPC, cluster, registry or state. Terraform builds the hub, Helm
installs the operator, and a throwaway workload generates the demand that makes
it rent machines.

Nothing here is part of the solution. `../src`, `../charts` and `../liqo-values`
are; this directory is the harness that runs them.

```
terraform/        the AWS hub: VPC, EKS, node group, addons
charts/inference/ a demo workload whose only job is to be unschedulable
Makefile          create, destroy, and the test scenarios
```

## Prerequisites

- `terraform`, `aws`, `kubectl`, `helm` on PATH, and credentials for the target
  account. `make preflight` checks them.
- `liqoctl`, matching the chart's Liqo version.
- **An S3 bucket for terraform state**, named `byon-ff-<your account id>`, in the
  backend's region. It is the one thing terraform cannot create for itself —
  it needs the bucket before it can hold its own state:

  ```bash
  aws s3 mb "s3://byon-ff-$(aws sts get-caller-identity --query Account --output text)" \
    --region eu-central-1
  ```

  The Makefile derives that name for you and passes it to `terraform init`;
  export `CURRENT_AWS_ACC_ID` to override.
- `../charts/values.local.yaml` — your provider accounts and pools. Copy it from
  `values.local.yaml.example`. Without it the operator installs cleanly and then
  crash-loops, naming the missing key in its log.
- A provider credentials Secret in the `external-node-autoscaler` namespace. `make up`
  reports which ones are missing at the end.
- Nothing to build: `make autoscaler` pulls the operator image CI published to
  `ghcr.io/haptikos/external-node-autoscaler`.

Optional: `terraform.tfvars`, copied from `terraform.tfvars.example`. Every
value has a working default.

## Create and destroy

```bash
make up      # terraform -> kubeconfig -> liqo -> charts
make down    # release rented machines, then tear the hub down
```

Both are idempotent and tolerate partial state: re-running `up` after a failure
resumes, and `down` never fails on things that are already gone.

**Order is not negotiable in `down`.** It drains first, because the autoscaler is
the only thing that hands rented machines back and it dies with the cluster.
Destroy the hub while a machine is peered and that machine keeps running — and
keeps billing — with nothing left alive to reap it.

`make help` lists every target.

## Trying it

```bash
make test-cpu      # a CPU-only pod asks for a machine. Rents a CPU MACHINE.
make test-gpu      # vLLM asks for a GPU. RENTS A GPU MACHINE.
make test-cleanup  # turn them off and watch the reap
```

`test-cpu` is the cheap rehearsal: the same loop end to end — Pending pod, rent,
K3s and Liqo join, peer, Running, idle, reap — against a 2 vCPU box from the
`cpu-dryrun` pool, which skips the NVIDIA stack and joins in about two minutes
instead of many. Prove the plumbing there before spending on an accelerator to
prove the same plumbing. Its pod fits exactly one per machine, so
`CPU_REPLICAS=4 make test-cpu` rents four and exercises the pool's
OVH-then-Vultr fall-through.

Both test targets name the full workload state, because `helm upgrade` re-renders
from `values.yaml` every time: `test-cpu` turns vLLM *off* explicitly, since the
chart default is on and a CPU test that forgot would rent a GPU too.

**How many replicas the hub can actually take.** Each peered machine costs two
pod slots on the *hub* — a `gw-` gateway and a `vk-` virtual kubelet — and the
cheap shape (3 x t3.small) allows 11 pods a node, 23 of which are already spoken
for. Ten free slots is five peerings. Machines rented past that boot fine and
then cannot peer: their gateway pod has nowhere to go, and `liqoctl peer` waits
for it.

The operator does not sit through that wait. It watches the gateway pod while
the handshake runs, and a pod the scheduler has *refused* — `PodScheduled=False`,
meaning it examined every node and wrote down why each one said no — is a final
answer, not a slow one. So it logs the verdict and kills the handshake on the
spot, then holds that machine back rather than making the identical doomed
attempt every pass. It resumes the moment the scheduler stops refusing that
gateway pod — add a hub node and the next pass peers — with
`blockedRetrySeconds` (10m) only as an upper bound. Peering is serial, so this is
what keeps one full hub from stalling the machines behind it — and with them the
labelling and scale-down that run in the same loop.

Note what it does *not* key on: how long the pod has been Pending. A gateway pod
pulling its image for the first time is Pending too, and is indistinguishable
from a refused one by age alone — abort on a stopwatch and you kill the healthy
one. `stuckPodSeconds` (5m) exists only for that ambiguous case, where no
verdict has been recorded and elapsed time is the only evidence there is, and it
never aborts anything; it just says so.

What it does *not* do is stop the overshoot. The machine is rented and billing
until `bootSeconds` reaps it. The log names the real constraint — pod slots, CPU,
memory, affinity, whatever the scheduler actually objected to — so raise
`node_count` or `node_instance_type` before raising `CPU_REPLICAS` much past
four.

`test-gpu` just applies the inference chart. There is no synthetic load
generator: vLLM's own `nodeSelector` and its `nvidia.com/gpu` request are the
demand, so what you are watching is what a real workload would do. The pod stays
Pending until a machine joins — that Pending *is* the signal the operator scales
on.

The autoscaler is never reconfigured by either target. Pools are declared
permanently and an idle pool rents nothing, so which one answers is decided
entirely by what the workload asks for. Pools, flavors and reserves come from
`make autoscaler` — edit a flavor, run a test, and you rent the old one.

`test-cleanup` removes the workload but leaves the NamespaceOffloading in place,
so the namespace stays offloaded and the next `test-gpu` needs no setup. Reaping
is not instant: the machine has to fall idle, then wait out `idleSeconds`.

## What it costs

Not free-tier. A new account is enough to *run* this, but EKS is billed from the
first minute: the control plane is about $0.10/hour, plus the node group and any
NAT gateways. The shipped `terraform.tfvars.example` picks the cheap shape —
`t3.small` nodes in public subnets, no NAT — and rented machines are billed by
their own provider on top.

`make down` is the bill switch. Run it when you stop, and check your providers'
consoles afterwards if it warned about virtual nodes it could not release.

Two AWS things outlive a careless teardown, and both keep charging. Rented
machines are the first, which is what `drain` is for. The second is the **ALB**
behind the gateway Ingress that `test-gpu` creates: only the
aws-load-balancer-controller can delete it, and it dies with the cluster, so
`uninstall-charts` deletes that Ingress and *waits* before terraform touches
anything. If it warns that the Ingress did not finish deleting, look under
**EC2 > Load Balancers** — a surviving ALB also blocks `make tf-destroy`,
because the security group the controller created for it is one terraform does
not know about:

```bash
vpc=$(terraform -chdir=terraform output -raw vpc_id)
aws elbv2 describe-load-balancers \
  --query "LoadBalancers[?VpcId=='$vpc'].[LoadBalancerName,LoadBalancerArn]" --output table
aws elbv2 delete-load-balancer --load-balancer-arn <arn>
```

Deleting the load balancer releases its security group a minute or so later;
re-run `make tf-destroy` after that.
