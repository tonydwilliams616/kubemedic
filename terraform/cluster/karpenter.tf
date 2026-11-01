# Karpenter (which adds a GPU node only while a pod needs one, and removes it when idle) and the NVIDIA device plugin (which tells Kubernetes the node has a GPU).

# Same pattern as the clinical-trial project's 30-platform stack, trimmed: one controller replica
# (a lab has one system node) and no Argo CD. The GPU NodePool itself is plain YAML in
# deploy/gpu/, applied by `make eks-up`, because Terraform can't plan a resource whose CRD is
# installed in the same apply.

# ---- AWS side: controller role (Pod Identity), node role + access entry, spot interruption queue ----
module "karpenter" {
  # Same pinned commit as the EKS module (release v21.26.0)
  source = "git::https://github.com/terraform-aws-modules/terraform-aws-eks.git//modules/karpenter?ref=323c633629b8e4306d94eb1392d7400a80143621"

  cluster_name = module.eks.cluster_name
  namespace    = "kube-system"

  create_pod_identity_association = true

  # The controller policy is longer than the 6,144-character limit for managed policies;
  # inline role policies allow 10,240
  enable_inline_policy = true

  # Fixed name: deploy/gpu/nodepool.yaml refers to it
  node_iam_role_use_name_prefix = false
  node_iam_role_name            = "${var.cluster_name}-karpenter-node"
}

# ---- cluster side ----
# CRDs as their own release: Helm never upgrades CRDs bundled inside a chart
resource "helm_release" "karpenter_crd" {
  name       = "karpenter-crd"
  namespace  = "kube-system"
  repository = "oci://public.ecr.aws/karpenter"
  chart      = "karpenter-crd"
  version    = var.karpenter_version
}

resource "helm_release" "karpenter" {
  name       = "karpenter"
  namespace  = "kube-system"
  repository = "oci://public.ecr.aws/karpenter"
  chart      = "karpenter"
  version    = var.karpenter_version
  skip_crds  = true
  wait       = true

  values = [yamlencode({
    settings = {
      clusterName       = module.eks.cluster_name
      clusterEndpoint   = module.eks.cluster_endpoint
      interruptionQueue = module.karpenter.queue_name # ~2 minutes' warning of a spot reclaim
    }

    serviceAccount = {
      name = module.karpenter.service_account
    }

    # One replica: the lab has a single system node. Karpenter runs there, never on a node it
    # manages (it would be removing its own host).
    replicas     = 1
    nodeSelector = { "node-role" = "system" }

    # Start without depending on in-cluster DNS (CoreDNS)
    dnsPolicy = "Default"

    controller = {
      resources = {
        requests = { cpu = "250m", memory = "512Mi" }
        limits   = { memory = "512Mi" }
      }
    }
  })]

  depends_on = [helm_release.karpenter_crd, module.karpenter]
}

# Advertises each GPU to Kubernetes as the resource nvidia.com/gpu, so a pod can ask for one.
# The node image (EKS AL2023 NVIDIA variant, chosen by Karpenter for GPU instances) already has
# the driver; this DaemonSet is the missing link. Its default affinity only targets nodes
# labelled nvidia.com/gpu.present=true, which only the GPU NodePool sets, and it tolerates the
# GPU taint.
resource "helm_release" "nvidia_device_plugin" {
  name       = "nvidia-device-plugin"
  namespace  = "kube-system"
  repository = "https://nvidia.github.io/k8s-device-plugin"
  chart      = "nvidia-device-plugin"
  version    = var.nvidia_device_plugin_version
  wait       = false # no GPU node exists until a pod asks for one

  depends_on = [helm_release.karpenter]
}
