variable "region" {
  description = "AWS region for the hub cluster."
  type        = string
  default     = "eu-central-1"
}

variable "cluster_name" {
  description = "Name of the EKS hub cluster."
  type        = string
  default     = "liqo-gpu-hub"
}

variable "cluster_version" {
  description = <<-EOT
    Kubernetes version of the hub control plane.

    Keep the reaper's kubectl in step when bumping this: charts/liqo-pod-reaper
    pins `alpine/k8s:<minor>` and kubectl only supports ±1 minor of skew
    against the API server.
  EOT
  type        = string
  default     = "1.36"
}

variable "cluster_support_type" {
  description = <<-EOT
    EKS upgrade policy.

    "STANDARD" (default here) refuses to enter extended support: when the
    version reaches end of standard support AWS auto-upgrades the control plane
    instead of silently moving you onto the higher extended-support rate. You
    never get billed the premium, but you also do not get to sit on an old
    minor indefinitely.

    "EXTENDED" is the AWS default and the opposite trade.
  EOT
  type        = string
  default     = "STANDARD"

  validation {
    condition     = contains(["STANDARD", "EXTENDED"], var.cluster_support_type)
    error_message = "cluster_support_type must be STANDARD or EXTENDED."
  }
}

variable "project_tag" {
  description = "Value of the `project` tag applied to every resource."
  type        = string
  default     = "liqo-gpu-bridge"
}

variable "vpc_cidr" {
  description = <<-EOT
    CIDR block for the hub VPC. With the VPC CNI this is also the hub's pod
    CIDR, which is what Liqo is told about — so it must not overlap anything on
    the K3s side.

    Avoid the obvious choices: K3s defaults to 10.42.0.0/16 for pods and
    10.43.0.0/16 for services, and 10.0.0.0/16 is a common OVH private-network
    default. Liqo can remap overlapping ranges, but that is friction worth
    skipping.
  EOT
  type        = string
  default     = "10.90.0.0/16"
}

variable "azs_count" {
  description = "Number of availability zones to spread the VPC across."
  type        = number
  default     = 2
}

variable "use_private_subnets" {
  description = <<-EOT
    true  (production shape): hub nodes sit in private subnets behind a single
          NAT gateway. Nothing is reachable from the internet.
    false (cheap test shape): hub nodes sit in public subnets with auto-assigned
          public IPs and NO NAT gateway is created — the NAT is the largest
          fixed hourly cost of an idle pilot.

    Safe either way for this topology: the Liqo gateway *server* lives on the
    K3s side, so the hub only ever needs egress and never accepts inbound
    connections. The node security group opens nothing to 0.0.0.0/0 in either
    mode. Flipping this replaces the node group.
  EOT
  type        = bool
  default     = true
}

variable "node_instance_type" {
  description = <<-EOT
    Instance type for the hub managed node group.

    Sizes the hub's POD-SLOT budget, which is what limits how many rented
    machines can be peered at once — not CPU or memory. With the VPC CNI, max
    pods per node comes from the instance's ENI/IP limits (t3.small: 11,
    t3.medium: 17), and every peering costs two hub pods: a `gw-<machine>`
    gateway and a `vk-<machine>` virtual kubelet.

    3 x t3.small is 33 slots, ~23 of them taken by kube-system, liqo and the
    operator: five concurrent peerings. Machines rented beyond that boot and
    then fail to peer, holding the operator's single-threaded loop for the full
    liqoctl timeout each while they bill. Raise this, or node_count, before
    raising a pool's max_machines.
  EOT
  type        = string
  default     = "t3.medium"
}

variable "node_count" {
  description = <<-EOT
    Size of the hub managed node group. min == max == desired on purpose: the
    hub must never autoscale. GPU capacity arrives as Liqo virtual nodes, not
    as EC2 instances.
  EOT
  type        = number
  default     = 2
}

variable "node_disk_size" {
  description = "Root volume size (GiB) for hub nodes."
  type        = number
  default     = 30
}

variable "cluster_authentication_mode" {
  description = <<-EOT
    How the cluster authorises Kubernetes API callers.

    "API" (default) uses EKS access entries only, so cluster access is IAM and
    Terraform all the way down — one source of truth, auditable in a plan, with
    no ConfigMap that can be hand-edited into a lockout.

    "API_AND_CONFIG_MAP" additionally honours the legacy aws-auth ConfigMap.
    The only foreseeable reason to want it here is peering this hub with
    another EKS cluster, where Liqo's AWS path hands out IAM-user credentials
    that may need mapping.

    "CONFIG_MAP" is deliberately not accepted: it is legacy-only, and EKS will
    not let a cluster move back to it.

    Widening (API -> API_AND_CONFIG_MAP) is not possible either — EKS only
    allows the transition toward API. Choose before you create the cluster.
  EOT
  type        = string
  default     = "API"

  validation {
    condition     = contains(["API", "API_AND_CONFIG_MAP"], var.cluster_authentication_mode)
    error_message = "cluster_authentication_mode must be API or API_AND_CONFIG_MAP."
  }
}

variable "cluster_admin_include_root" {
  description = <<-EOT
    Grant the AWS account root user cluster-admin via an access entry.

    Root is the one principal that can never be deleted and can never lose its
    IAM permissions, so it is the last identity standing if every other admin
    entry is wrong. Allowed as a STANDARD access entry: the EKS docs exclude
    only service-linked roles and STS session principals, and state that
    STANDARD entries accept "every IAM principal type".

    Worth knowing before relying on it as your break-glass: with
    authentication_mode = "API" you cannot actually lock yourself out of this
    cluster. Access entries are managed through the AWS API, not through
    Kubernetes, so ANY IAM principal holding eks:CreateAccessEntry and
    eks:AssociateAccessPolicy can mint itself a fresh admin entry. Root buys
    belt-and-braces, not a capability you would otherwise lack — weighed
    against a standing cluster-admin path for the account's most dangerous
    credential.
  EOT
  type        = bool
  default     = true
}

variable "cluster_admin_principals" {
  description = <<-EOT
    Extra IAM principals granted cluster-admin, as {label = principal_arn}.

    `enable_cluster_creator_admin_permissions` only covers the identity that
    ran `terraform apply`. Anyone else — including *you* browsing the EKS
    console as an SSO role rather than the apply identity — gets an empty
    Resources/Compute tab reading "This cluster does not have any Nodes, or you
    don't have permission to view them", because the console queries the
    Kubernetes API as the logged-in principal.

    For IAM Identity Center, use the role ARN, not the assumed-role session
    ARN:  arn:aws:iam::<account>:role/AWSReservedSSO_<permset>_<suffix>
  EOT
  type        = map(string)
  default     = {}
}

variable "cluster_endpoint_public_access_cidrs" {
  description = "CIDRs allowed to reach the public EKS API endpoint."
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "virtual_node_label_key" {
  description = <<-EOT
    Node label that marks a Liqo virtual node. System DaemonSets are fenced off
    any node carrying it (see addons.tf). Liqo sets `liqo.io/type=virtual-node`.
  EOT
  type        = string
  default     = "liqo.io/type"
}
