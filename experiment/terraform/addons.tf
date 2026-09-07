locals {
  # Default node affinity shared by the upstream aws-vpc-cni and
  # eks-pod-identity-agent charts, plus our virtual-node fence.
  fenced_node_affinity = {
    nodeAffinity = {
      requiredDuringSchedulingIgnoredDuringExecution = {
        nodeSelectorTerms = [
          {
            matchExpressions = [
              {
                key      = "kubernetes.io/os"
                operator = "In"
                values   = ["linux"]
              },
              {
                key      = "kubernetes.io/arch"
                operator = "In"
                values   = ["amd64", "arm64"]
              },
              {
                key      = "eks.amazonaws.com/compute-type"
                operator = "NotIn"
                values   = ["fargate", "hybrid", "auto"]
              },
              # The fence.
              {
                key      = var.virtual_node_label_key
                operator = "DoesNotExist"
              },
            ]
          }
        ]
      }
    }
  }
}

data "aws_eks_addon_version" "vpc_cni" {
  addon_name         = "vpc-cni"
  kubernetes_version = module.eks.cluster_version
  most_recent        = true
}

data "aws_eks_addon_version" "kube_proxy" {
  addon_name         = "kube-proxy"
  kubernetes_version = module.eks.cluster_version
  most_recent        = true
}

data "aws_eks_addon_version" "pod_identity_agent" {
  addon_name         = "eks-pod-identity-agent"
  kubernetes_version = module.eks.cluster_version
  most_recent        = true
}

data "aws_eks_addon_version" "coredns" {
  addon_name         = "coredns"
  kubernetes_version = module.eks.cluster_version
  most_recent        = true
}

resource "aws_eks_addon" "vpc_cni" {
  cluster_name  = module.eks.cluster_name
  addon_name    = "vpc-cni"
  addon_version = data.aws_eks_addon_version.vpc_cni.version

  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "OVERWRITE"
  preserve                    = false

  configuration_values = jsonencode({
    affinity = local.fenced_node_affinity
  })

  tags = local.tags
}

# The one add-on with no `affinity` in configuration_values, and not by
# choice: kube-proxy's schema is `additionalProperties: false` over
# {conntrack, ipvs, mode, podAnnotations, podLabels, resources}. Passing an
# affinity is rejected by the EKS API, and taints do not help either — the
# DaemonSet tolerates {"operator": "Exists"}.
#
# Fenced out of band by `make kube-proxy-fence`, which `make up` runs. This
# add-on carries resolve_conflicts_on_update = OVERWRITE, so a version bump
# reverts that patch: re-run the target after one.
resource "aws_eks_addon" "kube_proxy" {
  cluster_name  = module.eks.cluster_name
  addon_name    = "kube-proxy"
  addon_version = data.aws_eks_addon_version.kube_proxy.version

  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "OVERWRITE"
  preserve                    = false

  tags = local.tags
}

resource "aws_eks_addon" "pod_identity_agent" {
  cluster_name  = module.eks.cluster_name
  addon_name    = "eks-pod-identity-agent"
  addon_version = data.aws_eks_addon_version.pod_identity_agent.version

  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "OVERWRITE"
  preserve                    = false

  configuration_values = jsonencode({
    affinity = local.fenced_node_affinity
  })

  tags = local.tags

  depends_on = [module.eks_managed_node_group]
}

resource "aws_eks_addon" "coredns" {
  cluster_name  = module.eks.cluster_name
  addon_name    = "coredns"
  addon_version = data.aws_eks_addon_version.coredns.version

  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "OVERWRITE"
  preserve                    = false

  tags = local.tags

  depends_on = [module.eks_managed_node_group]
}
