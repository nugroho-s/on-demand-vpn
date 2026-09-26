terraform {
  required_version = ">= 1.5.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
  zone    = var.zone
}

# Enable required APIs
resource "google_project_service" "enabled" {
  for_each = toset([
    "compute.googleapis.com",
    "iam.googleapis.com",
    "run.googleapis.com",
    "cloudscheduler.googleapis.com",
    "pubsub.googleapis.com",
    "secretmanager.googleapis.com",
  ])

  service            = each.key
  disable_on_destroy = false
}

# --- Networking ---

resource "google_compute_network" "vpc" {
  name                    = "vpn-vpc"
  auto_create_subnetworks = false
}

resource "google_compute_subnetwork" "subnet" {
  name          = "vpn-subnet"
  ip_cidr_range = "10.12.0.0/28"
  region        = var.region
  network       = google_compute_network.vpc.id
}

resource "google_compute_firewall" "wireguard" {
  name      = "allow-wireguard"
  network   = google_compute_network.vpc.name
  direction = "INGRESS"
  priority  = 1000

  allow {
    protocol = "udp"
    ports    = [var.wg_port]
  }

  source_ranges = ["0.0.0.0/0"]
  target_tags   = ["wireguard"]
}

resource "google_compute_firewall" "ssh" {
  name      = "allow-ssh-iap"
  network   = google_compute_network.vpc.name
  direction = "INGRESS"
  priority  = 1000

  allow {
    protocol = "tcp"
    ports    = ["22"]
  }

  source_ranges = local.ssh_ranges
  target_tags   = ["wireguard"]
}

# --- Service accounts ---

# VM service account: can stop itself when idle
resource "google_service_account" "vm" {
  account_id   = "vpn-vm"
  display_name = "On-demand VPN VM"
}

resource "google_project_iam_member" "vm_stop_self" {
  project = var.project_id
  role    = "roles/compute.instanceAdmin"
  member  = "serviceAccount:${google_service_account.vm.email}"

  condition {
    title       = "vpn-vm-can-stop-itself"
    description = "Only the on-demand-vpn instance"
    expression  = "resource.type == 'compute.googleapis.com/Instance' && resource.name.endsWith('/instances/${local.instance_name}')"
  }
}

# Access to metadata-based server identity is implicit; no other roles needed.

# Bot service account: start/stop the VPN VM
resource "google_service_account" "bot" {
  account_id   = "vpn-bot"
  display_name = "On-demand VPN Discord bot"
}

# Per-instance SA cannot be granted; use project-level instanceAdmin scoped by
# an IAM condition on the instance name (unique to this stack).
resource "google_project_iam_member" "bot_compute" {
  project = var.project_id
  role    = "roles/compute.instanceAdmin.v1"
  member  = "serviceAccount:${google_service_account.bot.email}"

  condition {
    title       = "vpn-bot-manage-vpn-vm"
    description = "Only the on-demand-vpn instance"
    expression  = "resource.type == 'compute.googleapis.com/Instance' && resource.name.endsWith('/instances/${local.instance_name}')"
  }
}

# --- Secrets ---

resource "google_secret_manager_secret" "bot_token" {
  secret_id = "vpn-discord-bot-token"

  replication {
    auto {}
  }
}

resource "google_secret_manager_secret_version" "bot_token" {
  secret_data = var.discord_bot_token
  secret      = google_secret_manager_secret.bot_token.id
}

resource "google_secret_manager_secret_iam_member" "bot_token" {
  secret_id = google_secret_manager_secret.bot_token.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.bot.email}"
}

# --- WireGuard configuration ---

locals {
  instance_name = "on-demand-vpn"
}

data "google_compute_image" "debian" {
  family  = "debian-12"
  project = "debian-cloud"
}

resource "google_compute_instance" "vpn" {
  name         = local.instance_name
  machine_type = var.machine_type
  zone         = var.zone

  tags = ["wireguard"]

  boot_disk {
    initialize_params {
      image = data.google_compute_image.debian.self_link
      size  = 10
      type  = "pd-balanced"
    }
  }

  network_interface {
    subnetwork = google_compute_subnetwork.subnet.id

    # Ephemeral public IP (assigned while RUNNING, released on stop).
    # Without this block the VM would have no external IP at all.
    access_config {}
  }

  service_account {
    email  = google_service_account.vm.email
    scopes = ["https://www.googleapis.com/auth/cloud-platform"]
  }

  metadata_startup_script = templatefile("${path.module}/templates/startup.sh.tftpl", {
    wg_port      = var.wg_port
    wg_network   = var.wg_network
    wg_server_ip = local.wg_server_ip
    wg_config = templatefile("${path.module}/templates/wg0.conf.tftpl", {
      server_private_key = var.wg_server_private_key
      wg_port            = var.wg_port
      wg_mtu             = var.wg_mtu
      server_ip          = local.wg_server_ip
      peer_cidrs         = local.peer_cidrs
      peer_public_keys   = [for p in var.wg_peers : p.public_key]
    })
    idle_shutdown_minutes = var.idle_shutdown_minutes
    project_id            = var.project_id
    zone                  = var.zone
    instance_name         = local.instance_name
  })

  # Cheap trick: allow stopping for cost saving
  scheduling {
    preemptible       = false
    automatic_restart = true
  }

  # Do not auto-restart the VM on host maintenance if it was stopped on purpose.
  desired_status = "TERMINATED"

  depends_on = [google_project_service.enabled]
}

# --- Cloud Run bot ---

resource "google_cloud_run_v2_service" "bot" {
  name     = "vpn-bot"
  location = var.region

  template {
    service_account = google_service_account.bot.email

    scaling {
      min_instance_count = 0
      max_instance_count = 1
    }

    # CPU only allocated during requests (scale-to-zero friendly, v2 default)
    timeout = "300s"

    containers {
      image = var.bot_image

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
        startup_cpu_boost = false
      }

      env {
        name  = "GCP_PROJECT"
        value = var.project_id
      }
      env {
        name  = "GCP_ZONE"
        value = var.zone
      }
      env {
        name  = "INSTANCE_NAME"
        value = local.instance_name
      }
      env {
        name  = "DISCORD_PUBLIC_KEY"
        value = var.discord_public_key
      }
      env {
        name  = "ALLOWED_USER_IDS"
        value = join(",", var.allowed_discord_user_ids)
      }

      env {
        name = "DISCORD_BOT_TOKEN"

        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.bot_token.secret_id
            version = "latest"
          }
        }
      }
      env {
        name  = "DISCORD_APP_ID"
        value = var.discord_app_id
      }
    }
  }

  deletion_protection = false

  depends_on = [google_project_service.enabled]
}

# --- Nightly shutdown via Pub/Sub -> Cloud Run ---

resource "google_pubsub_topic" "vpn_stop" {
  name = "vpn-nightly-stop"
}

# Discord (unauthenticated internet) must invoke the bot; auth is Discord's
# Ed25519 signature. Pub/Sub also invokes for the nightly stop. One authoritative
# policy with BOTH members (an additive member resource would be overwritten).
data "google_iam_policy" "bot_invokers" {
  binding {
    role = "roles/run.invoker"
    members = [
      "allUsers",
      "serviceAccount:service-${data.google_project.project.number}@gcp-sa-pubsub.iam.gserviceaccount.com",
    ]
  }
}

data "google_project" "project" {
}

resource "google_cloud_run_v2_service_iam_policy" "pubsub_push" {
  name        = google_cloud_run_v2_service.bot.name
  location    = google_cloud_run_v2_service.bot.location
  policy_data = data.google_iam_policy.bot_invokers.policy_data
}

resource "google_pubsub_subscription" "vpn_stop" {
  name  = "vpn-nightly-stop-push"
  topic = google_pubsub_topic.vpn_stop.id

  push_config {
    oidc_token {
      service_account_email = google_service_account.bot.email
      audience              = google_cloud_run_v2_service.bot.uri
    }
    push_endpoint = "${google_cloud_run_v2_service.bot.uri}/pubsub/stop"
  }

  retry_policy {
    minimum_backoff = "10s"
    maximum_backoff = "300s"
  }

  expiration_policy {
    ttl = ""
  }
}

resource "google_cloud_scheduler_job" "nightly_stop" {
  name             = "vpn-nightly-stop"
  region           = var.region
  description      = "Nightly shutdown of the on-demand VPN VM"
  schedule         = var.scheduler_cron
  time_zone        = var.scheduler_timezone
  attempt_deadline = "320s"

  pubsub_target {
    topic_name = google_pubsub_topic.vpn_stop.id
    data       = base64encode(jsonencode({ action = "stop", source = "scheduler" }))
  }

  depends_on = [google_project_service.enabled]
}
