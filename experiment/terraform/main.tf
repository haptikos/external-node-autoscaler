data "aws_availability_zones" "available" {
  state = "available"
}

data "aws_caller_identity" "current" {}

data "aws_partition" "current" {}

locals {
  tags = {
    project = var.project_tag
  }

  root_admin = var.cluster_admin_include_root ? {
    root = "arn:${data.aws_partition.current.partition}:iam::${data.aws_caller_identity.current.account_id}:root"
  } : {}

  cluster_admins = merge(local.root_admin, var.cluster_admin_principals)

  azs = slice(data.aws_availability_zones.available.names, 0, var.azs_count)

  workload_subnets = var.use_private_subnets ? module.vpc.private_subnets : module.vpc.public_subnets
}

module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "~> 6.6"

  name = "${var.cluster_name}-vpc"
  cidr = var.vpc_cidr

  azs = local.azs

  private_subnets = var.use_private_subnets ? [for i in range(var.azs_count) : cidrsubnet(var.vpc_cidr, 4, i)] : []
  public_subnets  = [for i in range(var.azs_count) : cidrsubnet(var.vpc_cidr, 4, i + var.azs_count)]

  enable_nat_gateway = var.use_private_subnets
  single_nat_gateway = var.use_private_subnets

  map_public_ip_on_launch = !var.use_private_subnets

  enable_dns_hostnames = true
  enable_dns_support   = true

  public_subnet_tags = {
    "kubernetes.io/role/elb" = 1
  }

  private_subnet_tags = {
    "kubernetes.io/role/internal-elb" = 1
  }

  tags = local.tags
}

module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "~> 21.24"

  name               = var.cluster_name
  kubernetes_version = var.cluster_version

  vpc_id     = module.vpc.vpc_id
  subnet_ids = local.workload_subnets

  endpoint_public_access       = true
  endpoint_public_access_cidrs = var.cluster_endpoint_public_access_cidrs
  endpoint_private_access      = true

  upgrade_policy = {
    support_type = var.cluster_support_type
  }

  # Addons are managed as standalone aws_eks_addon resources (see addons.tf) so
  # that the DaemonSet fence lives in addon configuration and survives updates.
  addons = {}

  authentication_mode = var.cluster_authentication_mode

  enable_cluster_creator_admin_permissions = true

  access_entries = {
    for label, arn in local.cluster_admins : label => {
      principal_arn = arn
      type          = "STANDARD"
      policy_associations = {
        admin = {
          policy_arn = "arn:${data.aws_partition.current.partition}:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"
          access_scope = {
            type = "cluster"
          }
        }
      }
    }
  }

  create_kms_key              = false
  encryption_config           = null
  create_cloudwatch_log_group = false
  enabled_log_types           = []

  node_security_group_tags = {
    "kubernetes.io/cluster/${var.cluster_name}" = null
  }

  tags = local.tags
}

module "eks_managed_node_group" {
  source  = "terraform-aws-modules/eks/aws//modules/eks-managed-node-group"
  version = "~> 21.24"

  name = "${var.cluster_name}-base"

  use_name_prefix = true

  cluster_name         = module.eks.cluster_name
  kubernetes_version   = var.cluster_version
  cluster_service_cidr = module.eks.cluster_service_cidr

  subnet_ids = local.workload_subnets

  min_size     = var.node_count
  max_size     = var.node_count
  desired_size = var.node_count

  ami_type       = "AL2023_x86_64_STANDARD"
  instance_types = [var.node_instance_type]

  block_device_mappings = {
    root = {
      device_name = "/dev/xvda"
      ebs = {
        volume_size           = var.node_disk_size
        volume_type           = "gp3"
        encrypted             = true
        delete_on_termination = true
      }
    }
  }

  cluster_primary_security_group_id = module.eks.cluster_primary_security_group_id
  vpc_security_group_ids            = [module.eks.node_security_group_id]

  labels = {
    "node.liqo-gpu-bridge/role" = "hub"
  }

  tags = local.tags

  depends_on = [aws_eks_addon.vpc_cni]
}
