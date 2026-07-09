variable "stravit_email" {
  type        = string
  description = "Email to log into Stravit (optional master credentials)"
  default     = ""
  sensitive   = true
}

variable "stravit_password" {
  type        = string
  description = "Password to log into Stravit (optional master credentials)"
  default     = ""
  sensitive   = true
}
