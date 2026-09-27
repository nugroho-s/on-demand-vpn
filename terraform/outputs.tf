output "vpn_instance_name" {
  description = "Name of the VPN VM"
  value       = "on-demand-vpn"
}

output "vpn_instance_template" {
  description = "Self link of the VPN instance template"
  value       = google_compute_instance_template.vpn.self_link
}

output "wireguard_port" {
  description = "UDP port for WireGuard"
  value       = var.wg_port
}

output "wg_mtu" {
  description = "WireGuard MTU"
  value       = var.wg_mtu
}

output "bot_service_uri" {
  description = "Cloud Run service URI for the Discord bot"
  value       = google_cloud_run_v2_service.bot.uri
}

output "pubsub_topic" {
  description = "Pub/Sub topic used by the nightly stop scheduler"
  value       = google_pubsub_topic.vpn_stop.id
}

output "scheduler_job_name" {
  description = "Cloud Scheduler job name"
  value       = google_cloud_scheduler_job.nightly_stop.name
}

output "peer_ips" {
  description = "Map of peer name -> assigned WireGuard IP (for scripts/make-client-configs.sh)"
  value       = { for p in var.wg_peers : p.name => "${cidrhost(var.wg_network, p.ip_suffix)}/32" }
}

output "peers" {
  description = "Peer details incl. public keys, for scripts/make-client-configs.sh"
  value = [
    for p in var.wg_peers : {
      name       = p.name
      public_key = p.public_key
      ip         = "${cidrhost(var.wg_network, p.ip_suffix)}/32"
    }
  ]
}

output "wg_subnet" {
  description = "WireGuard tunnel subnet"
  value       = var.wg_network
}
