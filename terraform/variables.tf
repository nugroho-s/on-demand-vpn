variable "project_id" {
  description = "GCP project ID"
  type        = string
}

variable "region" {
  description = "GCP region for Cloud Run, Scheduler, Pub/Sub"
  type        = string
  default     = "asia-southeast1"
}

variable "zone" {
  description = "GCP zone for the VPN VM"
  type        = string
  default     = "asia-southeast1-b"
}

variable "bot_image" {
  description = "Container image for the Discord bot (Cloud Run)"
  type        = string
}

variable "discord_public_key" {
  description = "Discord application public key (hex) for interaction signature verification"
  type        = string
  sensitive   = true
}

variable "discord_bot_token" {
  description = "Discord bot token (stored in Secret Manager)"
  type        = string
  sensitive   = true
}

variable "wg_server_private_key" {
  description = "WireGuard server private key (base64). Generate: wg genkey"
  type        = string
  sensitive   = true
}

variable "wg_port" {
  description = "WireGuard UDP port"
  type        = number
  default     = 51820
}

variable "wg_mtu" {
  description = "WireGuard interface MTU. GCE NIC MTU is 1460; 1460 - 60 (WG overhead) = 1400"
  type        = number
  default     = 1400
}

variable "wg_network" {
  description = "WireGuard interface subnet (CIDR)"
  type        = string
  default     = "10.13.0.0/24"
}

variable "wg_server_ip_suffix" {
  description = "Last octet of the WireGuard server IP inside wg_network"
  type        = number
  default     = 1
}

variable "wg_peers" {
  description = "List of WireGuard peers. Generate per device: umask 077; wg genkey | tee private.key | wg pubkey > public.key"
  type = list(object({
    name        = string
    public_key  = string
    ip_suffix   = number
    dns_servers = optional(string, "1.1.1.1, 1.0.0.1")
  }))
  default = []
}

variable "allowed_discord_user_ids" {
  description = "Discord user IDs allowed to control the VPN (empty = allow anyone, NOT recommended)"
  type        = list(string)
  default     = []
}

variable "scheduler_cron" {
  description = "Cron schedule (UTC) for nightly VPN shutdown"
  type        = string
  default     = "0 18 * * *" # 18:00 UTC = 01:00 ICT
}

variable "scheduler_timezone" {
  description = "Timezone for the nightly shutdown schedule"
  type        = string
  default     = "Asia/Bangkok"
}

variable "idle_shutdown_minutes" {
  description = "Stop the VM if no WireGuard handshake for this many minutes"
  type        = number
  default     = 30
}

variable "machine_type" {
  description = "GCE machine type for the VPN VM"
  type        = string
  default     = "e2-small"
}

variable "ssh_source_ranges" {
  description = "Source IP ranges allowed for SSH. Empty = IAP-only (35.235.240.0/20)"
  type        = list(string)
  default     = []
}

locals {
  wg_server_ip = cidrhost(var.wg_network, var.wg_server_ip_suffix)
  # Peer IPs as /32 CIDRs for wg0.conf AllowedIPs
  peer_cidrs = [for p in var.wg_peers : "${cidrhost(var.wg_network, p.ip_suffix)}/32"]
  ssh_ranges = length(var.ssh_source_ranges) > 0 ? var.ssh_source_ranges : ["35.235.240.0/20"]
}
