#!/bin/bash
# What a rented machine does to itself, start to finish.
#
# Shared by every raw-VM provider, next to cloudinit.py for the same reason:
# nothing in here is provider-specific. Every input arrives through the env file
# sourced below, so this is a real script that `bash -n` and shellcheck read
# directly, and that a non-cloud-init provider could execute over SSH unchanged.
# A provider needing its own may ship providers/<name>/provision.sh and point
# `provisionScript` at it.
#
# Everything lives in one script rather than a list of runcmd entries because
# cloud-init shellifies runcmd WITHOUT `set -e`: a failure halfway through is
# ignored, the script keeps going, and the module reports SUCCESS as long as the
# last command exits 0. A machine can then look perfectly booted while having
# installed nothing.
set -euxo pipefail

# NAME, GPU, LIQO_VERSION, HUB_EGRESS_IPS. Written by cloudinit.py.
# shellcheck disable=SC1091  # generated at boot, not present in the repo
. /root/provision.env

# cloud-init runs runcmd with a bare environment. `k3s kubectl` needs HOME or it
# dies with "$HOME is not defined", and KUBECONFIG saves every later invocation
# from guessing.
export HOME=${HOME:-/root}
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml

# Bounded waits. An `until ...; do sleep; done` loop with no deadline turns a
# hard failure into an indistinguishable-from-slow hang, and the operator cannot
# tell the difference either — it just waits out bootSeconds.
wait_for() {
  local timeout=$1 what=$2; shift 2
  local deadline=$(( SECONDS + timeout ))
  until "$@"; do
    if (( SECONDS > deadline )); then
      echo "TIMEOUT after ${timeout}s waiting for: ${what}" >&2
      return 1
    fi
    sleep 5
  done
}

gpu_visible() {
  local n
  n=$(k3s kubectl get node "$NAME" \
        -o jsonpath='{.status.allocatable.nvidia\.com/gpu}' 2>/dev/null || true)
  [ -n "$n" ] && [ "$n" != "0" ]
}

# Some providers attach the routable address straight to the interface rather
# than as an OpenStack floating IP, so the EC2-compat `public-ipv4` key is EMPTY
# and the address turns up under `local-ipv4` instead (OVH does this; Vultr
# reports it either way). Try both, then fall back to the source address of the
# default route, and reject anything RFC1918 — a private address here silently
# produces a kubeconfig the hub can never reach.
is_routable() {
  local ip=${1:-}
  [[ $ip =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || return 1
  case $ip in
    10.*|127.*|169.254.*|192.168.*) return 1 ;;
    172.1[6-9].*|172.2[0-9].*|172.3[01].*) return 1 ;;
  esac
  return 0
}

route_ip() {
  local ip
  ip=$(ip -4 route get 1.1.1.1 2>/dev/null \
         | sed -n 's/.*src \([0-9.]*\).*/\1/p')
  is_routable "$ip" && { echo "$ip"; return 0; }
  return 1
}

detect_ip() {
  local ip
  for key in public-ipv4 local-ipv4; do
    ip=$(curl -sf --max-time 5 --retry 5 --retry-delay 3 \
           "http://169.254.169.254/latest/meta-data/$key" 2>/dev/null || true)
    is_routable "$ip" && { echo "$ip"; return 0; }
  done
  route_ip
}

PUBLIC_IP=$(detect_ip) || {
  echo "could not determine a routable address for this machine" >&2
  exit 1
}
# Second SAN candidate: any address this box can legitimately be reached on
# belongs in the certificate. Falls back to PUBLIC_IP.
ROUTE_IP=$(route_ip || echo "$PUBLIC_IP")
echo "using public address ${PUBLIC_IP} (route source ${ROUTE_IP})"

while fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1; do sleep 5; done
DEBIAN_FRONTEND=noninteractive apt-get update

pkgs=(curl ufw)
if [ "$GPU" = true ]; then
  pkgs+=(nvidia-driver-550-server nvidia-utils-550-server)
fi
DEBIAN_FRONTEND=noninteractive apt-get install -y "${pkgs[@]}"

# The model cache lives here (the demo workload's vllm.modelCache.hostPath). Created
# unconditionally: an empty directory costs nothing on a CPU box, and making it
# conditional would couple this branch to another chart's values.
mkdir -p /opt/models

if [ "$GPU" = true ]; then
  curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
    | gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
  curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
    | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
    > /etc/apt/sources.list.d/nvidia-container-toolkit.list
  DEBIAN_FRONTEND=noninteractive apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y nvidia-container-toolkit

  # apt installs the module, it does not LOAD it, and this box is never
  # rebooted. Fail here rather than 600s later in gpu_visible: an unloaded
  # module and a mis-wired container runtime both present as "no GPU on the
  # node", and only this check tells them apart.
  modprobe nvidia
  nvidia-smi -L
fi

# k3s SERVER, not agent: this box is a cluster of one. On GPU the driver is
# installed first so k3s auto-detects the nvidia runtime. The CAs written by
# cloud-init are already in place, so k3s skips generating its own and mints its
# serving cert from ours.
#
# --tls-san carries BOTH addresses we can see: the operator picks the one it
# dials from the provider's API, not from this box, and a disagreement would
# surface as a TLS failure minutes into a peering, reading like a network fault.
curl -sfL https://get.k3s.io \
  | INSTALL_K3S_CHANNEL=stable sh -s - server \
      --tls-san "$PUBLIC_IP" --tls-san "$ROUTE_IP"
wait_for 300 "k3s api readyz" k3s kubectl get --raw /readyz

if [ "$GPU" = true ]; then
  # k3s REGISTERS the nvidia containerd runtime when it finds the toolkit at
  # install time; it does not make it the default. Only a diagnostic --
  # gpu_visible below is the real gate -- but without it the failure is silent,
  # see the patch two blocks down.
  grep -rq nvidia /var/lib/rancher/k3s/agent/etc/containerd/ \
    || echo "WARNING: no nvidia runtime in k3s containerd config" >&2

  # Idempotent: recent k3s creates this RuntimeClass itself, older does not.
  # Heredoc body and terminator stay at column 0 inside this block — <<'EOF'
  # requires the terminator to start the line.
  k3s kubectl apply -f - <<'EOF'
apiVersion: node.k8s.io/v1
kind: RuntimeClass
metadata:
  name: nvidia
handler: nvidia
EOF

  # There is no shared cluster to inherit a device-plugin DaemonSet from any
  # more: every machine installs its own, or its GPU is invisible to Kubernetes
  # and the ResourceSlice advertises zero.
  #
  # The upstream static manifest requests no RuntimeClass, because it assumes a
  # host where `nvidia-ctk runtime configure --set-as-default` has run. On k3s it
  # therefore lands on runc, cannot see the driver, and NVML init fails -- and
  # since that same manifest sets FAIL_ON_INIT_ERROR=false, the pod reports
  # Running while advertising zero GPUs. Nothing looks broken. Pinning the
  # DaemonSet to the nvidia handler is what actually wires it up.
  k3s kubectl apply -f https://raw.githubusercontent.com/NVIDIA/k8s-device-plugin/v0.17.0/deployments/static/nvidia-device-plugin.yml
  k3s kubectl -n kube-system patch daemonset nvidia-device-plugin-daemonset \
    --type merge \
    -p '{"spec":{"template":{"spec":{"runtimeClassName":"nvidia"}}}}'
  wait_for 600 "nvidia.com/gpu on node $NAME" gpu_visible
fi

# Liqo, pinned to the hub's version. Skew between the two sides of a peering
# fails late and confusingly.
curl -sfL "https://github.com/liqotech/liqo/releases/download/${LIQO_VERSION}/liqoctl-linux-amd64.tar.gz" \
  | tar -xz -C /tmp liqoctl
install -m 0755 /tmp/liqoctl /usr/local/bin/liqoctl
KUBECONFIG=/etc/rancher/k3s/k3s.yaml liqoctl install k3s \
  --cluster-id "$NAME" \
  --api-server-url "https://${PUBLIC_IP}:6443" \
  --timeout 10m

# Everything the hub needs, scoped to where the hub actually comes from. Empty
# means "from anywhere", which is what public-subnet mode gets: there the hub
# egresses from each node's own auto-assigned public IP, which changes whenever a
# node is replaced, so an allowlist would break the peering at the least
# convenient moment. With a single NAT gateway the address is stable and worth
# pinning.
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp

# 6443 is NOT optional and is easy to mistake for peering-only traffic: the hub's
# virtual kubelet creates the remote pod, reflects its status and streams its
# logs through this port for the life of the peering. 30000:32767/udp carries the
# WireGuard tunnel to the gateway server. 10250 may well be unnecessary — Liqo
# reads logs via the remote API server, not this — but it is scoped rather than
# removed until that is confirmed on a live peering.
for rule in "6443/tcp" "30000:32767/udp" "10250/tcp"; do
  port=${rule%/*}; proto=${rule#*/}
  if [ -n "$HUB_EGRESS_IPS" ]; then
    # shellcheck disable=SC2086  # deliberate split: a space-separated list
    for ip in $HUB_EGRESS_IPS; do
      ufw allow from "$ip" to any port "$port" proto "$proto"
    done
  else
    ufw allow "$port/$proto"
  fi
done
ufw --force enable

# The user data carries this cluster's CA private keys and the metadata service
# hands it to any container that asks. k3s keeps the same keys on disk anyway, so
# this does not stop a process that has already escaped — it closes the cheaper
# SSRF-shaped path. Needs iptables rather than ufw because pod-CIDR traffic is
# not "incoming".
iptables -I FORWARD -d 169.254.169.254 -j DROP
iptables -I OUTPUT -d 169.254.169.254 -m owner ! --uid-owner 0 -j DROP

# THE READINESS SIGNAL, and the last thing this script does. Everything that must
# be true before a peering happens ABOVE this line. Moving it earlier does not
# make the machine available sooner — it makes the hub peer with one that is not
# finished, and a peering completed before the device plugin lands advertises
# zero GPUs for that machine's life. Cross-tree contract with
# autoscaler/labels.py; `make lint` compares them.
k3s kubectl label node "$NAME" \
  external-node-autoscaler/bootstrap=complete --overwrite
echo "provision complete"
