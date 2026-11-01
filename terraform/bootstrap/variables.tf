# Inputs for the bootstrap stack: names, the account it must run in, and the budget alert.

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

variable "monthly_budget_usd" {
  description = "Monthly spend that triggers alerts (at 50% actual and 100% forecast)."
  type        = number
  default     = 30
}

variable "alert_email" {
  description = "Where budget alerts go. Not stored in git: pass it with TF_VAR_alert_email or at the prompt."
  type        = string

  validation {
    condition     = can(regex("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$", var.alert_email))
    error_message = "alert_email must be an email address."
  }
}
