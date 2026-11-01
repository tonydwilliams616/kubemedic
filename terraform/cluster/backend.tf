# Keeps this stack's state in the bootstrap stack's S3 bucket, with S3's native lock file instead of a DynamoDB table.

terraform {
  backend "s3" {
    bucket       = "kubemedic-tfstate-766704559857"
    key          = "cluster/terraform.tfstate"
    region       = "us-east-1"
    encrypt      = true
    use_lockfile = true
  }
}
