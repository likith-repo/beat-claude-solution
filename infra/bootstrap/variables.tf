variable "region" {
  type    = string
  default = "ap-southeast-1"
}

variable "github_sub_prefix" {
  description = "Start of the OIDC sub claim. This repo uses GitHub's immutable subject (owner and repo ids), see: gh api repos/<owner>/<repo>/actions/oidc/customization/sub"
  type        = string
  default     = "repo:likith-repo@309335415/beat-claude-solution@1431751865"
}

variable "create_oidc_provider" {
  description = "false if this account already has a token.actions.githubusercontent.com provider"
  type        = bool
  default     = true
}
