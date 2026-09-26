# AGENTS.md

## What this is

On-demand WireGuard VPN on GCP, started/stopped only via a Discord bot. Terraform manages all infra; bot runs on Cloud Run (scale to zero); VM has an ephemeral public IP and auto-stops when idle.

## Commands

- Terraform is managed via tfenv (linuxbrew) but may not be on PATH in this session. Use `/home/linuxbrew/.linuxbrew/bin/terraform` if `terraform` is not found. Version is pinned in `terraform/.terraform-version` (1.16.4).
- `terraform -chdir=terraform fmt && terraform -chdir=terraform validate` — run after every tf change. There is no test suite; validation + template render checks are the only checks.
- Render startup script locally to syntax-check bash without deploying:
  `terraform -chdir=terraform console -var-file=<tfvars> <<< 'templatefile("templates/startup.sh.tftpl", {wg_port=51820, wg_network="10.13.0.0/24", wg_server_ip="10.13.0.1", wg_config="X", idle_shutdown_minutes=30, project_id="p", zone="z", instance_name="i"})' | bash -n`
- Bot image: `docker build -t asia-southeast1-docker.pkg.dev/jahyadi/on-demand-vpn/vpn-bot:latest bot/ && docker push ...` (project: `jahyadi`). Any `bot/main.py` change requires rebuild+push+`terraform apply`.
- Client configs: `bash scripts/make-client-configs.sh` (matches local keys to tf peers by PUBLIC KEY, not name).

## Hard-won gotchas

- **Template escaping**: `.tftpl` files mix Terraform `${}` with bash `${}`. Bash ones must be escaped as `$${}`. Always `bash -n` the rendered startup script after edits.
- **wg0.conf.tftpl whitespace**: use `%{~ for ...}` then `%{ endfor ~}` (trim markers) or the render produces broken `PrivateKey = KEY# peer 0` lines.
- **GCE IAM conditions**: `resource.name` for instances is a full path (`projects/x/zones/y/instances/name`) — match with `endsWith('/instances/on-demand-vpn')`, never `startsWith('project-name')`.
- **Cloud Run invoker IAM**: do NOT use both `google_cloud_run_v2_service_iam_member` (allUsers) and a `..._iam_policy` resource on the same service — the authoritative policy silently deletes the member binding and Discord gets 403 "didn't respond in time". Use ONE authoritative policy with all members.
- **`-replace` on the Cloud Run service nukes its IAM policy server-side** but the `..._iam_policy` terraform resource shows no diff, so it is never reapplied — every request 403s. Always pair `-replace=google_cloud_run_v2_service.bot` with `-replace=google_cloud_run_v2_service_iam_policy.pubsub_push`, or verify with `gcloud run services get-iam-policy` after.
- **Cloud Run serves by digest, terraform tracks the tag string**: repushing `:latest` with unchanged tfvars creates NO new revision. Force one with `-replace=google_cloud_run_v2_service.bot` (see the IAM gotcha above).
- **GCE MTU**: NIC MTU is 1460, so WireGuard needs `MTU = 1400` (1460-60) or clients handshake but can't browse.
- **Discord API via Python**: default `urllib` User-Agent is blocked by Cloudflare (error 1010). Set a custom User-Agent in every call (`bot/main.py`, `bot/register_commands.py`).
- **Discord 3s interaction window**: `/vpn start` responds immediately and PATCHes `@original` later — don't block the webhook on the GCE start call.
- **Ephemeral IP**: no `access_config {}` on the instance = no external IP at all. Empty `access_config {}` is intentional.
- **Startup script ordering**: `/etc/iptables/` only exists after `iptables-persistent` is installed — install it before writing `rules.v4` (script has `set -e`).
- VM idle-check stops itself via metadata-token `curl` to the compute API — gcloud CLI is NOT installed on the VM.
- Deploy order matters: gen keys → build/push image → `terraform apply` → paste `bot_service_uri`/interactions URL into Discord → `register_commands.py` → `/vpn start`.

## Layout

- `terraform/` — all GCP infra (single stack, no modules). `templates/*.tftpl` rendered into VM startup script.
- `bot/` — Flask app deployed to Cloud Run; also `register_commands.py` (run locally with `DISCORD_BOT_TOKEN` + `DISCORD_APP_ID`).
- `scripts/` — key generation and client config rendering (run from repo root).
- `server-key/`, `client-configs/`, `terraform.tfvars` are gitignored secrets — never commit, never print.

## Repo state

- Git remote: `github.com/nugroho-s/on-demand-vpn` (SSH). Sign commits with GPG key `7A6A33A257AD9FC5` (`commit.gpgsign` enabled); author identity is `massatriya@gmail.com`.
- Do not commit/push without being asked.
