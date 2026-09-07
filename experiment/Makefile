# Create/destroy orchestration for the experiment harness.
#
#   make up     empty AWS account -> hub ready for peering
#   make down   everything gone, nothing billing
#
# Both are idempotent and tolerate partial state: re-running `up` after a
# failure resumes, and `down` never fails on things that are already gone.
# Order matters and is not negotiable — see README, "Create and destroy order".

SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help

TF_DIR              ?= terraform
# The solution: the operator chart, its source, and the Liqo values. Outside
# experiment/ because they are what is being demonstrated, not the harness.
AUTOSCALER_CHART    ?= ../charts
LIQO_VALUES         ?= ../liqo-values/values-eks.yaml
# The demo workload. Lives here because it exists only to generate demand.
INFERENCE_CHART     ?= charts/inference

# Local, gitignored overlay holding your provider account settings. Created
# from ../charts/values.local.yaml.example.
AUTOSCALER_VALUES   ?= $(AUTOSCALER_CHART)/values.local.yaml

LIQO_VERSION        ?= v1.2.0

# aws-load-balancer-controller. Pinned like everything else here: the chart
# tracks the controller's own version, and a floating tag means a controller
# upgrade lands during an unrelated `make up`.
#
# A helm install rather than an EKS add-on because AWS publishes no add-on for
# it — `aws eks describe-addon-versions` lists none under any owner — and it
# lives here rather than in terraform so `down` keeps working against a cluster
# that is already gone.
ALB_CONTROLLER_VERSION ?= 3.5.0
LIQO_NAMESPACE      ?= liqo
AUTOSCALER_NS       ?= external-node-autoscaler
INFERENCE_NS        ?= inference

# MUST match the inference chart's `gateway.namespace`: teardown finds the
# ALB-backed Ingress by it.
GATEWAY_NS          ?= inference-gw
IMAGE_TAG           ?= latest

# Kubeconfig for a hand-managed K3s cluster. Only `liqoctl unpeer` needs it;
# rented machines peer and unpeer themselves.
K3S_KUBECONFIG      ?= k3s.yaml

# How long `make down` waits for the autoscaler to release rented machines.
DRAIN_TIMEOUT       ?= 300

# How long `make down` waits for the gateway's ALB. Overrun it and the load
# balancer outlives the cluster that knew how to delete it.
ALB_TIMEOUT         ?= 180

# Replicas ARE the demand: one pending pod is one unit in the pool it selects.
GPU_REPLICAS        ?= 1
# Same for the CPU stand-in, and it goes further: the cpu-dryrun pool's machines
# fit exactly one of these pods each, so N replicas rents N machines and
# `CPU_REPLICAS=4 make test-cpu` is how the pool's OVH-then-Vultr fall-through
# gets exercised without buying a GPU to do it.
CPU_REPLICAS        ?= 1

# The secrets `next-steps` checks for, read out of the values file rather than
# listed here: a hardcoded list goes stale the moment a second provider is
# added, and it goes stale silently — the check reports all-clear while the
# operator CrashLoopBackOffs on a credential nobody was asked for.
_VALUES_FOR_SECRETS  = $(if $(wildcard $(AUTOSCALER_VALUES)),$(AUTOSCALER_VALUES),$(AUTOSCALER_CHART)/values.yaml)
PROVIDER_SECRETS    ?= $(shell awk '/^[[:space:]]*credentialsSecret:/ {print $$2}' $(_VALUES_FOR_SECRETS) 2>/dev/null | tr -d '"' | sort -u)

TF = terraform -chdir=$(TF_DIR)

# The state bucket is per account. Lazily expanded, so nothing calls AWS unless
# a terraform target actually runs; override by exporting CURRENT_AWS_ACC_ID.
CURRENT_AWS_ACC_ID  ?= $(shell aws sts get-caller-identity --query Account --output text 2>/dev/null)
TF_INIT = $(TF) init -input=false -backend-config="bucket=byon-ff-$(CURRENT_AWS_ACC_ID)"

# Reading an output from a destroyed/empty state must not abort the Makefile —
# `down` has to work against a half-gone stack. Nothing is silently discarded:
# stderr goes to a file so tf_explain can show it when a required value is empty.
TF_ERR := /tmp/.byon-tf-err
tf_out  = $$($(TF) output -raw $(1) 2>$(TF_ERR) || true)

# Why an output came back empty. `make` runs a non-interactive shell and
# inherits only exported environment variables — never aliases or shell
# functions — so a `tf` alias or an aws-vault wrapper that works when you type
# it by hand is simply not here.
define tf_explain
echo "make: terraform output '$(1)' came back empty."; \
echo "  cwd:       $$(pwd)"; \
echo "  command:   $(TF) output -raw $(1)"; \
echo "  terraform: $$(command -v terraform || echo 'NOT ON PATH')"; \
echo "  AWS_PROFILE=$${AWS_PROFILE:-<unset>} AWS_REGION=$${AWS_REGION:-<unset>}"; \
if [ -s $(TF_ERR) ]; then echo "  terraform said:"; sed 's/^/    /' $(TF_ERR); \
else echo "  terraform exited quietly — the output is probably not in state yet."; fi; \
echo "  note: make does not inherit shell aliases or functions. If you normally"; \
echo "        run terraform through a wrapper, its credentials are absent here."; \
exit 1
endef

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

################################################################################
# up
################################################################################

.PHONY: up
up: preflight tf-apply kubeconfig kube-proxy-fence liqo-install charts next-steps ## Full create: terraform -> liqo -> charts

.PHONY: preflight
preflight: ## Verify the toolchain and the liqoctl/chart version lock
	@missing=0; \
	for t in terraform aws kubectl helm; do \
	  command -v $$t >/dev/null 2>&1 || { echo "MISSING: $$t"; missing=1; }; \
	done; \
	if command -v liqoctl >/dev/null 2>&1; then \
	  v=$$(liqoctl version --client 2>/dev/null | grep -oE 'v[0-9]+\.[0-9]+\.[0-9]+' | head -1); \
	  if [ "$$v" != "$(LIQO_VERSION)" ]; then \
	    echo "WARNING: liqoctl $$v != chart $(LIQO_VERSION). They must match on BOTH clusters."; \
	  fi; \
	else \
	  echo "NOTE: liqoctl not installed — needed for the manual peering step."; \
	fi; \
	[ $$missing -eq 0 ] || { echo "install the missing tools and retry"; exit 1; }

.PHONY: tf-init
tf-init: ## terraform init
	@[ -n "$(CURRENT_AWS_ACC_ID)" ] || { \
	  echo "CURRENT_AWS_ACC_ID is empty — the state bucket name needs it."; \
	  echo "Export it, or fix your AWS credentials so 'aws sts get-caller-identity' works."; \
	  exit 1; }
	$(TF_INIT)

.PHONY: tf-apply
tf-apply: tf-init ## terraform apply
	$(TF) apply -auto-approve -input=false

.PHONY: kubeconfig
kubeconfig: ## Point local kubectl at the hub
	@cmd="$(call tf_out,update_kubeconfig_command)"; \
	[ -n "$$cmd" ] || { $(call tf_explain,update_kubeconfig_command); }; \
	echo "+ $$cmd"; eval "$$cmd"

.PHONY: kube-proxy-fence
kube-proxy-fence: ## Keep kube-proxy off Liqo virtual nodes (the one add-on terraform cannot fence)
	@# terraform/addons.tf fences vpc-cni and eks-pod-identity-agent through an
	@# `affinity` block in configuration_values. kube-proxy's add-on schema is
	@# `additionalProperties: false` over {conntrack, ipvs, mode, podAnnotations,
	@# podLabels, resources} — no affinity knob — so it is patched here instead.
	@#
	@# Taints are not the alternative: the shipped DaemonSet tolerates
	@# {"operator": "Exists"}, so no taint holds it back. Node affinity is the
	@# only lever. Dropping a node out of the affinity is also the cleanup — the
	@# controller then deletes the pod stuck on it.
	@#
	@# RE-RUN AFTER A VERSION BUMP: the add-on carries
	@# resolve_conflicts_on_update = OVERWRITE, which restores the unfenced
	@# template. `make up` re-runs this for you.
	@terms=$$(kubectl -n kube-system get ds kube-proxy \
	    -o jsonpath='{.spec.template.spec.affinity.nodeAffinity.requiredDuringSchedulingIgnoredDuringExecution.nodeSelectorTerms[*].matchExpressions[*].key}' \
	    2>/dev/null) || { \
	  echo "kube-proxy DaemonSet not found in kube-system — is the add-on installed?"; \
	  exit 1; }; \
	if [ -z "$$terms" ]; then \
	  echo "== kube-proxy: setting the virtual-node fence (it ships no nodeAffinity) =="; \
	  kubectl -n kube-system patch daemonset kube-proxy --type=merge -p \
	    '{"spec":{"template":{"spec":{"affinity":{"nodeAffinity":{"requiredDuringSchedulingIgnoredDuringExecution":{"nodeSelectorTerms":[{"matchExpressions":[{"key":"liqo.io/type","operator":"DoesNotExist"}]}]}}}}}}}'; \
	elif printf '%s\n' $$terms | grep -qx 'liqo.io/type'; then \
	  echo "kube-proxy: already fenced off virtual nodes"; \
	else \
	  echo "== kube-proxy: appending the virtual-node fence to its nodeAffinity =="; \
	  kubectl -n kube-system patch daemonset kube-proxy --type=json -p \
	    '[{"op":"add","path":"/spec/template/spec/affinity/nodeAffinity/requiredDuringSchedulingIgnoredDuringExecution/nodeSelectorTerms/0/matchExpressions/-","value":{"key":"liqo.io/type","operator":"DoesNotExist"}}]'; \
	fi

.PHONY: liqo-install
liqo-install: ## Install/upgrade Liqo on the hub with the pinned values
	@endpoint="$(call tf_out,cluster_endpoint)"; \
	pod_cidr="$(call tf_out,vpc_cidr)"; \
	svc_cidr="$(call tf_out,cluster_service_cidr)"; \
	[ -n "$$endpoint" ] || { $(call tf_explain,cluster_endpoint); }; \
	helm repo add liqo https://helm.liqo.io/ >/dev/null 2>&1 || true; \
	helm repo update liqo >/dev/null; \
	helm upgrade --install liqo liqo/liqo \
	  --version $(LIQO_VERSION) \
	  --namespace $(LIQO_NAMESPACE) --create-namespace \
	  -f $(LIQO_VALUES) \
	  --set-string apiServer.address="$$endpoint" \
	  --set-string ipam.podCIDR="$$pod_cidr" \
	  --set-string ipam.serviceCIDR="$$svc_cidr" \
	  --disable-openapi-validation \
	  --wait --timeout 15m

.PHONY: alb-controller
alb-controller: ## Install/upgrade aws-load-balancer-controller (creates the `alb` IngressClass)
	@# Required by the inference chart's gateway. Without it the Ingress is
	@# created and NOTHING reconciles it: no ALB, no events, no error. That
	@# silence is why this is a target rather than a documented step.
	@#
	@# serviceAccount.name and the namespace must match the pod-identity
	@# association in terraform/alb-controller.tf, or every AWS call fails with
	@# AccessDenied. vpcId is explicit because discovery is unreliable and a wrong
	@# value surfaces as an ALB stuck "provisioning".
	@#
	@# NOT fenced off virtual nodes: it does not need to be while every pool
	@# carries taints. But autoscaler/liqo.py REPLACES a virtual node's taints
	@# with its pool's, so a pool declared with no `taints:` yields an untainted
	@# node this could land on — and it would be torn down with the machine.
	@cluster="$(call tf_out,cluster_name)"; \
	vpc="$(call tf_out,vpc_id)"; \
	region="$(call tf_out,region)"; \
	[ -n "$$cluster" ] || { $(call tf_explain,cluster_name); }; \
	[ -n "$$vpc" ] || { $(call tf_explain,vpc_id); }; \
	helm repo add eks https://aws.github.io/eks-charts >/dev/null 2>&1 || true; \
	helm repo update eks >/dev/null; \
	helm upgrade --install aws-load-balancer-controller \
	  eks/aws-load-balancer-controller \
	  --version $(ALB_CONTROLLER_VERSION) \
	  --namespace kube-system \
	  --set-string clusterName="$$cluster" \
	  --set-string region="$$region" \
	  --set-string vpcId="$$vpc" \
	  --set serviceAccount.create=true \
	  --set-string serviceAccount.name=aws-load-balancer-controller \
	  --wait --timeout 5m
	@echo ""
	@echo "IngressClasses now available:"
	@kubectl get ingressclass 2>/dev/null || true

.PHONY: charts
charts: alb-controller autoscaler inference ## Install/upgrade all in-cluster charts

.PHONY: autoscaler
autoscaler: ## Install/upgrade the external-node-autoscaler
	@url="ghcr.io/haptikos/external-node-autoscaler"; \
	egress="$(call tf_out,hub_egress_ips)"; \
	extra=""; [ -f "$(AUTOSCALER_VALUES)" ] && extra="-f $(AUTOSCALER_VALUES)"; \
	[ -n "$$extra" ] || echo "NOTE: $(AUTOSCALER_VALUES) not found — using chart defaults. The chart passes provider settings through untouched, so this installs cleanly and then CrashLoopBackOffs; the operator log names the missing key."; \
	helm upgrade --install external-node-autoscaler $(AUTOSCALER_CHART) \
	  --namespace $(AUTOSCALER_NS) --create-namespace \
	  $$extra \
	  --set-string image.repository="$$url" \
	  --set-string image.tag="$(IMAGE_TAG)" \
	  --set-string config.liqoVersion="$(LIQO_VERSION)" \
	  --set-string config.hubEgressIps="$$egress"
	@# Empty is correct in public-subnet mode, where the hub has no stable egress
	@# address to pin — so this reports rather than fails.
	@egress="$(call tf_out,hub_egress_ips)"; \
	if [ -n "$$egress" ]; then \
	  echo "rented machines will allow the hub only from: $$egress"; \
	else \
	  echo "NOTE: no stable hub egress address (public-subnet mode) — rented"; \
	  echo "      machines will allow 6443/tcp and the WireGuard ports from"; \
	  echo "      ANY source. Set use_private_subnets = true to pin them."; \
	fi

.PHONY: inference
inference: ## Install/upgrade the offloaded namespace + vLLM
	helm upgrade --install inference $(INFERENCE_CHART) \
	  --namespace $(INFERENCE_NS) --create-namespace

.PHONY: next-steps
next-steps: ## Print the manual steps that remain
	@echo ""
	@echo "══════════════════════════════════════════════════════════════════════"
	@echo " Hub is up."
	@echo "══════════════════════════════════════════════════════════════════════"
	@echo ""
	@echo " 1. PROVIDER SECRET (nothing secret lives in this repo)"
	@for s in $(PROVIDER_SECRETS); do \
	  kubectl get secret $$s -n $(AUTOSCALER_NS) >/dev/null 2>&1 \
	    && echo "      ✓ $$s" || echo "      ✗ $$s — see README 'Secrets'"; \
	done
	@echo ""
	@echo "    Rented machines need no secret from you: each is its own K3s"
	@echo "    cluster and publishes its kubeconfig here on first boot."
	@echo ""
	@echo " 2. The operator image comes from GHCR, published by CI:"
	@echo "    ghcr.io/haptikos/external-node-autoscaler:$(IMAGE_TAG)"
	@echo ""
	@echo " CHECK:"
	@echo "      kubectl get nodes -l liqo.io/type=virtual-node"
	@echo "      kubectl get foreignclusters"
	@echo "      kubectl -n $(INFERENCE_NS) get pods -w"
	@echo ""

################################################################################
# down
################################################################################

.PHONY: down
down: drain uninstall-charts liqo-uninstall tf-destroy ## Full teardown, in the only safe order

.PHONY: drain
drain: ## Remove demand and let the autoscaler release rented machines before the hub dies
	@# First, always. The autoscaler is the only thing that hands rented machines
	@# back, and it dies with the cluster: destroy the hub while a machine is
	@# peered and that machine keeps running, and billing, with nothing alive to
	@# reap it.
	@echo "== draining rented capacity (up to $(DRAIN_TIMEOUT)s) =="
	@if ! kubectl cluster-info >/dev/null 2>&1; then \
	  echo "   hub unreachable — skipping (check your providers for orphaned instances by hand)"; exit 0; \
	fi; \
	helm uninstall inference --namespace $(INFERENCE_NS) >/dev/null 2>&1 || true; \
	if helm status external-node-autoscaler --namespace $(AUTOSCALER_NS) >/dev/null 2>&1; then \
	  helm upgrade external-node-autoscaler $(AUTOSCALER_CHART) \
	    --namespace $(AUTOSCALER_NS) --reuse-values \
	    --set config.idleSeconds=0 --set config.drain=true \
	    >/dev/null 2>&1 || true; \
	  deadline=$$(( $$(date +%s) + $(DRAIN_TIMEOUT) )); \
	  while [ $$(date +%s) -lt $$deadline ]; do \
	    n=$$(kubectl get nodes -l liqo.io/type=virtual-node --no-headers 2>/dev/null | wc -l | tr -d ' '); \
	    [ "$$n" = "0" ] && { echo "   all virtual nodes released"; break; }; \
	    echo "   $$n virtual node(s) left..."; sleep 15; \
	  done; \
	  n=$$(kubectl get nodes -l liqo.io/type=virtual-node --no-headers 2>/dev/null | wc -l | tr -d ' '); \
	  [ "$$n" = "0" ] || echo "   WARNING: $$n virtual node(s) still present — CHECK YOUR PROVIDERS FOR RUNNING INSTANCES"; \
	else \
	  echo "   no autoscaler release — nothing to drain"; \
	fi

.PHONY: uninstall-charts
uninstall-charts: ## helm uninstall the in-cluster charts (best effort)
	@echo "== uninstalling charts =="
	@helm uninstall inference --namespace $(INFERENCE_NS) 2>/dev/null || true
	@helm uninstall external-node-autoscaler --namespace $(AUTOSCALER_NS) 2>/dev/null || true
	@# Only the aws-load-balancer-controller can delete the ALB behind this
	@# Ingress, through a finalizer. `helm uninstall` does not wait for it and
	@# tf-destroy kills the controller, leaving an ALB that bills and a security
	@# group that blocks the VPC destroy. So wait here, while it is still alive.
	@echo "== releasing the gateway load balancer (up to $(ALB_TIMEOUT)s) =="
	@if kubectl get namespace $(GATEWAY_NS) >/dev/null 2>&1; then \
	  kubectl delete ingress --all --namespace $(GATEWAY_NS) --timeout=$(ALB_TIMEOUT)s \
	    || echo "   WARNING: the gateway Ingress did not finish deleting. CHECK EC2 > LOAD BALANCERS FOR A SURVIVING ALB -- it is still billing, and it will block 'make tf-destroy'"; \
	else \
	  echo "   no $(GATEWAY_NS) namespace -- nothing to release"; \
	fi
	@kubectl delete namespace $(GATEWAY_NS) --ignore-not-found --timeout=120s 2>/dev/null || true
	@kubectl delete namespace $(INFERENCE_NS) --ignore-not-found --timeout=120s 2>/dev/null || true
	@kubectl delete namespace $(AUTOSCALER_NS) --ignore-not-found --timeout=120s 2>/dev/null || true

.PHONY: liqo-uninstall
liqo-uninstall: ## Unpeer then remove Liqo (best effort)
	@echo "== unpeering and removing Liqo =="
	@if ! kubectl cluster-info >/dev/null 2>&1; then \
	  echo "   hub unreachable — skipping"; exit 0; \
	fi; \
	if command -v liqoctl >/dev/null 2>&1 && [ -f "$(K3S_KUBECONFIG)" ]; then \
	  liqoctl unpeer --remote-kubeconfig "$(K3S_KUBECONFIG)" --skip-confirm 2>/dev/null \
	    || echo "   liqoctl unpeer failed or nothing peered — continuing"; \
	fi; \
	if command -v liqoctl >/dev/null 2>&1; then \
	  liqoctl uninstall --skip-confirm 2>/dev/null || true; \
	fi; \
	helm uninstall liqo --namespace $(LIQO_NAMESPACE) 2>/dev/null || true

.PHONY: tf-destroy
tf-destroy: ## terraform destroy
	@echo "== terraform destroy =="
	@[ -d "$(TF_DIR)/.terraform" ] || $(TF_INIT)
	$(TF) destroy -auto-approve -input=false

################################################################################
# tests
################################################################################

.PHONY: test-cpu
test-cpu: ## Apply the inference chart CPU-only: a machine gets rented, no GPU. Rents a CPU MACHINE.
	@# The cheap half of test-gpu: the same loop -- Pending pod, rent, K3s + Liqo
	@# join, peer, Running, idle, reap -- against a 2 vCPU box that joins in about
	@# two minutes. Prove the plumbing here before spending on an accelerator.
	@#
	@# vLLM is turned OFF explicitly, not defensively: vllm.enabled defaults to
	@# TRUE, so a CPU test that only set cpu.enabled would also rent a GPU.
	@echo "== CPU-only workload on ($(CPU_REPLICAS) replica(s)) -- this rents a CPU machine, no GPU =="
	helm upgrade --install inference $(INFERENCE_CHART) \
	  --namespace $(INFERENCE_NS) --create-namespace \
	  --set vllm.enabled=false \
	  --set cpu.enabled=true \
	  --set cpu.replicas=$(CPU_REPLICAS)
	@echo ""
	@echo "Watch it work:"
	@echo "  kubectl -n $(AUTOSCALER_NS) logs -f deploy/external-node-autoscaler"
	@echo "  kubectl -n $(INFERENCE_NS) get pods -l app=cpu-demand -w"
	@echo "  kubectl get nodes -l liqo.io/type=virtual-node -L external-node-autoscaler/pool"
	@echo ""
	@echo "What to check:"
	@echo "  * the pod stays Pending until a machine joins -- that IS the demand"
	@echo "  * the pool column reads cpu-dryrun, not gpu"
	@echo "  * once Running, a timestamp lands every 5s and names the machine:"
	@echo "      kubectl -n $(INFERENCE_NS) logs -f deploy/cpu-demand -c heartbeat"
	@echo "    Liqo proxies that stream from the rented box, so a ticking clock"
	@echo "    is the control-plane path working, not just the pod existing."
	@echo "  * and the data path, which is a separate thing to be broken:"
	@echo "      kubectl -n $(INFERENCE_NS) port-forward svc/cpu-demand-svc 8080:8080"
	@echo "      curl -s -o /dev/null -w '%{http_code}\n' localhost:8080   # 200"
	@echo ""
	@echo "  CPU_REPLICAS=4 make test-cpu   one pod per machine: exercises the"
	@echo "                                 pool's OVH -> Vultr fall-through"
	@echo "  make test-cleanup              release them and watch the reap"

.PHONY: test-gpu
test-gpu: ## Apply the inference chart: vLLM asks for a GPU and one gets rented. RENTS A GPU MACHINE.
	@# The autoscaler is NOT reconfigured. Pools are declared permanently and an
	@# idle pool rents nothing, so the demand is entirely vLLM's nodeSelector plus
	@# its nvidia.com/gpu request -- exactly what a real workload would do.
	@echo "== vLLM on -- THIS RENTS A GPU MACHINE =="
	helm upgrade --install inference $(INFERENCE_CHART) \
	  --namespace $(INFERENCE_NS) --create-namespace \
	  --set vllm.enabled=true \
	  --set vllm.replicas=$(GPU_REPLICAS) \
	  --set cpu.enabled=false
	@echo ""
	@echo "Watch it work:"
	@echo "  kubectl -n $(AUTOSCALER_NS) logs -f deploy/external-node-autoscaler"
	@echo "  kubectl -n $(INFERENCE_NS) get pods -w"
	@echo "  kubectl get nodes -l liqo.io/type=virtual-node -L external-node-autoscaler/pool"
	@echo ""
	@echo "What to check:"
	@echo "  * the pod stays Pending until a machine joins -- that IS the demand"
	@echo "  * exactly one machine appears, showing pool=gpu. A blank column means"
	@echo "    the virtual node was never labelled."
	@echo "  * first start is slow: image and model weights download on a cold box."
	@echo ""
	@echo "  make test-cleanup    release it and watch the reap"

.PHONY: test-cleanup
test-cleanup: ## Turn the workloads off and wait for the machines to be reaped
	@# BOTH, always. `helm upgrade` re-renders from values.yaml plus these
	@# --sets, so anything not named here reverts to its chart default — and
	@# vllm.enabled defaults to TRUE. Dropping either flag from this line does
	@# not leave the other workload alone, it turns it back on.
	helm upgrade --install inference $(INFERENCE_CHART) \
	  --namespace $(INFERENCE_NS) --create-namespace \
	  --set vllm.enabled=false \
	  --set cpu.enabled=false
	@echo "== waiting for the autoscaler to reap the machines =="
	@# Not instant: the machine has to fall idle, then wait out idleSeconds
	@# before the operator releases it.
	@deadline=$$(( $$(date +%s) + $(DRAIN_TIMEOUT) )); \
	while [ $$(date +%s) -lt $$deadline ]; do \
	  n=$$(kubectl get nodes -l liqo.io/type=virtual-node --no-headers 2>/dev/null | wc -l | tr -d ' '); \
	  [ "$$n" = "0" ] && { echo "   released"; break; }; \
	  echo "   $$n virtual node(s) left..."; sleep 15; \
	done; \
	n=$$(kubectl get nodes -l liqo.io/type=virtual-node --no-headers 2>/dev/null | wc -l | tr -d ' '); \
	[ "$$n" = "0" ] || echo "   WARNING: $$n still present -- check idleSeconds, then your provider console"

