variable "region" {
  type    = string
  default = "ap-southeast-1"
}

variable "lambda_zip" {
  description = "Path to the zip made by scripts/build_lambda.py"
  type        = string
  default     = "../build/lambda.zip"
}

variable "allowed_origins" {
  description = "Browser origins allowed by CORS. The Streamlit app calls the API from its server, so no browser origin needs access; the x-api-key header is the real gate."
  type        = list(string)
  default     = ["*"]
}

variable "openrouter_model" {
  type    = string
  default = "nvidia/nemotron-3-super-120b-a12b"
}

variable "retention_days" {
  description = "Days before run files expire from S3"
  type        = number
  default     = 7
}

variable "max_searches_per_run" {
  type    = number
  default = 25
}
