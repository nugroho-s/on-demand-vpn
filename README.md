# On-Demand VPN

A cost-saving WireGuard VPN on GCP, controlled exclusively through a Discord bot.

- **WireGuard** server on a GCE VM (ephemeral public IP, UDP 51820)
- **Discord bot** on Cloud Run (scale-to-zero, free at this usage) — the only way to start/stop the VPN
- **Cloud Scheduler** nightly shutdown (cron) + **idle auto-shutdown** on the VM (no WireGuard handshake for N minutes)
- **Terraform** manages everything

## Architecture

```
You ──/vpn start──▶ Discord ──interaction webhook──▶ Cloud Run bot (Flask)
                                                     │ GCE API start/stop
                                                     ▼
                                            GCE VM (WireGuard, e2-small)
                                                     ▲
Cloud Scheduler ──cron──▶ Pub/Sub ──push──▶ bot ──stop──┘
systemd timer on VM ──no handshake N min──▶ gcloud stop (self)
```

Cost profile (asia-southeast1, e2-small): ~$0.00 compute while stopped, disk ~$0.40/mo, Cloud Run + Scheduler within free tier.

## Repo layout

```
terraform/                # all GCP infrastructure
  main.tf                 # VPC, VM, IAM, secrets, Cloud Run, Scheduler, Pub/Sub
  variables.tf, outputs.tf
  terraform.tfvars.example
  templates/startup.sh.tftpl   # WireGuard install + idle-shutdown timer
  templates/wg0.conf.tftpl     # rendered to /etc/wireguard/wg0.conf on boot
  templates/client.conf.tftpl
bot/                      # Discord bot (Cloud Run)
scripts/
  gen-keys.sh             # generate server + client WireGuard keypairs
  make-client-configs.sh  # render client .conf files after apply
```

## Setup

### 1. Discord application

1. Create an app at <https://discord.com/developers/applications> → add a Bot → copy the **bot token**.
2. Copy the **Public Key** (General Information page).
3. Copy the **Application ID**.
4. OAuth2 → URL Generator: scopes `bot` + `applications.commands`; invite the bot to your server.
5. After deploy: set the **Interactions Endpoint URL** to `https://<bot-service-uri>/interactions` (from terraform output `bot_service_uri`).

### 2. GCP

```bash
export PROJECT_ID=your-project
gcloud config set project $PROJECT_ID
```

### 3. Keys

```bash
scripts/gen-keys.sh 2        # server key + 2 client keypairs
cat server-key/private.key   # -> wg_server_private_key in tfvars
cat client-configs/peer-*/public.key
```

### 4. Build & push the bot image

```bash
cd bot
gcloud builds submit --tag gcr.io/$PROJECT_ID/vpn-bot   # or docker build + push
```

### 5. Terraform

```bash
cd terraform
cp terraform.tfvars.example terraform.tfvars   # fill in values
terraform init
terraform apply
terraform output bot_service_uri              # -> paste into Discord (step 1.5)
```

### 6. Register the slash command

```bash
cd bot
DISCORD_BOT_TOKEN=... DISCORD_APP_ID=... python register_commands.py
```

### 7. Client configs

```bash
scripts/make-client-configs.sh
# edit client-configs/peer-1/peer-1.conf: replace EPHEMERAL_IP with the IP
# the bot reports after /vpn start
```

## Usage

```
/vpn status   # VM state + current endpoint
/vpn start    # boot VM; bot edits its reply with the endpoint when up
/vpn stop     # stop VM
```

The public IP is **ephemeral** (changes on every start). After `/vpn start`, copy the reported endpoint into your client config. If this annoys you, switch to a static reserved IP (`google_compute_address`) — one-line change in `main.tf`.

Only user IDs in `allowed_discord_user_ids` may use the commands.

## Day-to-day operations

- **Add a device**: `scripts/gen-keys.sh 1`, add the pubkey to `wg_peers` in `terraform.tfvars` (next free `ip_suffix`), `terraform apply`, re-run `scripts/make-client-configs.sh`.
- **SSH (debug)**: `gcloud compute ssh on-demand-vpn --zone=asia-southeast1-b --tunnel-through-iap` (SSH is IAP-only by default).
- **Change nightly stop time**: `scheduler_cron` (UTC) in `terraform.tfvars`.
- **Change idle timeout**: `idle_shutdown_minutes` (default 30).

## Notes / limitations

- Peers are managed by Terraform only; manual `wg set` on the VM will drift.
- The VM is created `TERMINATED` (stopped) — run `/vpn start` once after first apply.
- Idle shutdown uses WireGuard handshakes: a connected-but-silent client may be cut off after 30 min without traffic (keepalive in client template prevents this while the tunnel is up).
