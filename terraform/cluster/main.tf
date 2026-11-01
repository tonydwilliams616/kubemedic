# The EPHEMERAL EKS cluster that will host the self-hosted model: `make eks-up` before a session, `make eks-down` after.

# Cost while up: control plane $0.10/h plus one small spot system node (~$0.02/h) and its public
# IP. GPU nodes are added by Karpenter (karpenter.tf), only while a pod asks for a GPU.
#
# This cluster only SERVES A MODEL. The agent and the executor keep working on the kind lab: the
# lab refuses any context that isn't kind-*, and the agent's and executor's RBAC identities exist
# only there. `make eks-up` also switches kubectl back to whatever context you were using.

data "aws_caller_identity" "current" {}

# The IAM role behind whoever runs `terraform apply` (your SSO role), named explicitly as the
# cluster admin below, rather than the module's implicit "creator is admin" setting.
data "aws_iam_session_context" "current" {
  arn = data.aws_caller_identity.current.arn
}

module "eks" {
  # Pinned to the commit of release v21.26.0 (same as the clinical-trial project)
  source = "git::https://github.com/terraform-aws-modules/terraform-aws-eks.git?ref=323c633629b8e4306d94eb1392d7400a80143621"

  name               = var.cluster_name
  kubernetes_version = var.kubernetes_version

  vpc_id     = module.vpc.vpc_id
  subnet_ids = module.vpc.public_subnets

  # Public endpoint so your laptop can reach the API (IAM auth on every call); private endpoint
  # so nodes talk to the control plane inside the VPC
  endpoint_public_access       = true
  endpoint_public_access_cidrs = var.api_allowed_cidrs
  endpoint_private_access      = true

  # Access entries only: no aws-auth ConfigMap to drift
  authentication_mode                      = "API"
  enable_cluster_creator_admin_permissions = false

  access_entries = {
    admin = {
      principal_arn = data.aws_iam_session_context.current.issuer_arn
      policy_associations = {
        admin = {
          policy_arn   = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"
          access_scope = { type = "cluster" }
        }
      }
    }
  }

  # Pod Identity instead of IRSA: no OIDC provider per cluster, simpler trust policies
  enable_irsa = false

  addons = {
    coredns    = { most_recent = true }
    kube-proxy = { most_recent = true }
    vpc-cni = {
      most_recent    = true
      before_compute = true # networking must exist before nodes join
    }
    eks-pod-identity-agent = {
      most_recent    = true
      before_compute = true # Karpenter (6.2b) gets its AWS credentials this way
    }
  }

  # EKS already encrypts Secrets at rest with an AWS-owned key; a customer-managed key would be
  # created and billed on every spin-up of a short-lived cluster
  create_kms_key    = false
  encryption_config = null

  # Control-plane logs off: EKS can re-create the log group after Terraform deletes it, which
  # breaks the next apply of an ephemeral cluster
  create_cloudwatch_log_group = false
  enabled_log_types           = []

  eks_managed_node_groups = {
    system = {
      ami_type       = "AL2023_x86_64_STANDARD"
      instance_types = var.system_node_instance_types
      capacity_type  = "SPOT" # ~70% cheaper; fine for a session-only cluster

      # One node is enough for CoreDNS and the Karpenter controller in a lab
      min_size     = 1
      max_size     = 2
      desired_size = 1

      labels = {
        "node-role" = "system"
      }
    }
  }

  # Karpenter finds the node security group by this tag (6.2b)
  node_security_group_tags = {
    "karpenter.sh/discovery" = var.cluster_name
  }

  # Order matters both ways. Up: the internet gateway route must exist before nodes start, or
  # they can't reach the internet to join the cluster. Down: nodes (and their public IPs) must be
  # gone before the internet gateway can be detached; without this Terraform deletes both at once
  # and the gateway sits retrying until the node group has finished deleting.
  depends_on = [module.vpc]
}
