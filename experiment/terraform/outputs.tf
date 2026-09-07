output "cluster_name" {
  description = "Name of the EKS hub cluster."
  value       = module.eks.cluster_name
}

output "region" {
  description = "AWS region the hub runs in."
  value       = var.region
}

output "update_kubeconfig_command" {
  description = "Command that points your local kubectl at the hub."
  value       = "aws eks update-kubeconfig --region ${var.region} --name ${module.eks.cluster_name}"
}
# --- consumed by the Makefile when installing the Liqo chart ------------------

output "cluster_endpoint" {
  description = "Hub API server endpoint. Becomes `apiServer.address` in the Liqo values."
  value       = module.eks.cluster_endpoint
}

output "cluster_certificate_authority_data" {
  description = <<-EOT
    Base64 hub CA. Handed to each rented machine so it can verify the hub when
    publishing its kubeconfig, instead of skipping TLS verification. This is a
    public certificate, not a credential.
  EOT
  value       = module.eks.cluster_certificate_authority_data
}

output "cluster_service_cidr" {
  description = "Service CIDR of the hub. Becomes `ipam.serviceCIDR` in the Liqo values."
  value       = module.eks.cluster_service_cidr
}

output "vpc_cidr" {
  description = <<-EOT
    VPC CIDR of the hub. With the VPC CNI, pods take VPC addresses, so this is
    what Liqo must treat as the pod CIDR (`ipam.podCIDR`) — matching what
    `liqoctl install eks` derives from DescribeVpcs.
  EOT
  value       = module.vpc.vpc_cidr_block
}

output "hub_egress_ips" {
  description = <<-EOT
    Addresses the hub's outbound traffic arrives from, space-separated. Becomes
    `config.hubEgressIps` on the autoscaler, which renders it into each rented
    machine's ufw allowlist for the k3s API and the WireGuard tunnel.

    With `use_private_subnets = true` this is the single NAT gateway's public
    IP: one stable address, worth pinning. In public-subnet mode it is
    deliberately EMPTY rather than a list of node IPs — those are auto-assigned
    and change whenever a node is replaced, so an allowlist built from them
    would sever an established peering the next time the node group rolls, long
    after anyone would connect the two events. Empty means "from anywhere".
  EOT
  value       = var.use_private_subnets ? join(" ", module.vpc.nat_public_ips) : ""
}

output "vpc_id" {
  description = <<-EOT
    VPC of the hub. aws-load-balancer-controller needs it explicitly (`--set
    vpcId`): it does not discover it reliably, and a wrong or missing value
    shows up as an ALB that never leaves "provisioning".
  EOT
  value       = module.vpc.vpc_id
}

output "network_mode" {
  description = "Which shape the VPC was built in — handy when wondering where the NAT bill went."
  value       = var.use_private_subnets ? "private subnets + 1 NAT gateway" : "public subnets, NO NAT gateway (cheap test mode)"
}

output "node_security_group_id" {
  description = "Security group attached to hub nodes."
  value       = module.eks.node_security_group_id
}
