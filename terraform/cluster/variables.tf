# Inputs for the ephemeral cluster stack: names, network range, Kubernetes version and node sizes.

variable "project_name" {
  description = "Prefix used for naming resources."
  type        = string
  default     = "kubemedic"
}

variable "aws_region" {
  description = "AWS region."
  type        = string
  default     = "us-east-1"
}

variable "aws_account_id" {
  description = "The kubemedic-dev account. The provider refuses to run against any other account."
  type        = string
  default     = "766704559857"
}

variable "cluster_name" {
  description = "EKS cluster name; also the Karpenter discovery tag on subnets and the node security group."
  type        = string
  default     = "kubemedic-eks"
}

variable "kubernetes_version" {
  description = "EKS Kubernetes version. Must be in STANDARD support: extended support costs 6x more per hour."
  type        = string
  default     = "1.36"
}

variable "vpc_cidr" {
  description = "CIDR for the VPC. Distinct from other projects' ranges so they could be peered later."
  type        = string
  default     = "10.30.0.0/16"
}

variable "az_ids" {
  # Zone IDs name the same physical zones in every account (us-east-1a etc. are shuffled per
  # account). Three zones give spot GPU capacity more places to come from. use1-az3 can't host
  # EKS control planes.
  description = "Availability zones by zone ID."
  type        = list(string)
  default     = ["use1-az1", "use1-az2", "use1-az4"]

  validation {
    condition     = length(var.az_ids) >= 2 && !contains(var.az_ids, "use1-az3")
    error_message = "Use at least 2 us-east-1 zone IDs, not use1-az3 (no EKS control planes there)."
  }
}

variable "api_allowed_cidrs" {
  description = "Who can reach the Kubernetes API over the internet (every call still needs IAM auth). Narrow to your IP/32 to harden."
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "system_node_instance_types" {
  description = "Small instance types for the system node group. Several similar types make spot capacity easier to get."
  type        = list(string)
  default     = ["t3.medium", "t3a.medium", "t3.large", "t3a.large"]
}

variable "karpenter_version" {
  description = "Karpenter chart version. Kubernetes 1.36 needs Karpenter >= 1.13 (karpenter.sh compatibility matrix)."
  type        = string
  default     = "1.14.1"
}

variable "nvidia_device_plugin_version" {
  description = "NVIDIA k8s-device-plugin chart version."
  type        = string
  default     = "0.19.3"
}
