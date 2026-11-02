# A small VPC with public subnets only and no NAT gateway, created and destroyed with the cluster.

# Why no private subnets and NAT: the GPU node downloads ~25 GB per start (the vLLM image and
# the model weights). Through a NAT gateway that is ~$1.10 of data processing every time, plus
# ~$0.05/h for the NAT itself. In public subnets nodes get their own public IP (~$0.005/h) and
# downloads cost nothing.
#
# The trade-off is exposure: nodes have public IPs. Inbound is still closed: the node security
# group (from the EKS module) only admits the control plane and other nodes, nothing from the
# internet, and the model server is reached only through `kubectl port-forward`. A production
# setup would use private subnets with a NAT gateway or VPC endpoints instead.
#
# Everything here is free while idle, but lives in this stack so `make eks-down` leaves nothing.

locals {
  # /20 public subnets (4,094 addresses each): EKS gives every pod a VPC address
  public_subnets = [for i, az in var.az_ids : cidrsubnet(var.vpc_cidr, 4, i)]
}

module "vpc" {
  # Pinned to the commit of release v6.7.3: a tag can be
  # re-pointed, a commit hash cannot (supply chain).
  source = "git::https://github.com/terraform-aws-modules/terraform-aws-vpc.git?ref=b3abd6df2ecf052451a361ed55b8f06f8742a795"

  name = "${var.project_name}-vpc"
  cidr = var.vpc_cidr
  azs  = var.az_ids

  public_subnets     = local.public_subnets
  enable_nat_gateway = false

  enable_dns_hostnames = true
  enable_dns_support   = true

  # Nodes need a public IP to reach the internet without a NAT gateway (see above)
  map_public_ip_on_launch = true

  # Lock down the default security group: no rules, so nothing can use it
  manage_default_security_group  = true
  default_security_group_ingress = []
  default_security_group_egress  = []

  public_subnet_tags = {
    "kubernetes.io/role/elb" = "1"
    "karpenter.sh/discovery" = var.cluster_name # Karpenter places GPU nodes in subnets with this tag
  }
}

# Free gateway endpoint: S3 traffic (including ECR image layers) stays on the AWS network
resource "aws_vpc_endpoint" "s3" {
  vpc_id            = module.vpc.vpc_id
  service_name      = "com.amazonaws.${var.aws_region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = module.vpc.public_route_table_ids

  tags = {
    Name = "${var.project_name}-s3-endpoint"
  }
}
