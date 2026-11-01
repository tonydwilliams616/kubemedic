# Values other tools need after `make eks-up`: the cluster's name, endpoint and how to reach it.

output "cluster_name" {
  description = "EKS cluster name."
  value       = module.eks.cluster_name
}

output "cluster_endpoint" {
  description = "EKS API endpoint."
  value       = module.eks.cluster_endpoint
}

output "admin_principal" {
  description = "The IAM role given cluster-admin (whoever applied this stack)."
  value       = data.aws_iam_session_context.current.issuer_arn
}

output "kubeconfig_command" {
  description = "Adds the cluster to your kubeconfig as context kubemedic-eks."
  value       = "aws eks update-kubeconfig --name ${module.eks.cluster_name} --region ${var.aws_region} --alias kubemedic-eks"
}
