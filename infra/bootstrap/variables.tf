variable "region" {
  type    = string
  default = "ap-southeast-1"
}

variable "github_repo" {
  description = "owner/name of the repo whose workflows may assume the CI roles"
  type        = string
  default     = "likith-repo/beat-claude-solution"
}

variable "create_oidc_provider" {
  description = "false if this account already has a token.actions.githubusercontent.com provider"
  type        = bool
  default     = true
}
