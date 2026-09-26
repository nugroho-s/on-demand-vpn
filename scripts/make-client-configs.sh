#!/usr/bin/env bash
# Render client .conf files from local keys + terraform outputs.
#
# Usage:
#   scripts/gen-keys.sh                  # first: create server-key/ and client-configs/
#   terraform -chdir=terraform apply
#   scripts/make-client-configs.sh
#
# Endpoint is a placeholder (EPHEMERAL_IP) because the VM's public IP changes
# on every start — replace it with the IP the bot reports after /vpn start.
set -euo pipefail

cd "$(dirname "$0")/.."

command -v terraform >/dev/null || { echo "terraform not found"; exit 1; }
[ -f server-key/public.key ] || { echo "run scripts/gen-keys.sh first"; exit 1; }

SERVER_PUB="$(cat server-key/public.key)"
WG_PORT="$(terraform -chdir=terraform output -raw wireguard_port)"
OUT=client-configs

for dir in "$OUT"/*/; do
  name="$(basename "$dir")"
  [ -f "$dir/private.key" ] || continue
  ip="$(terraform -chdir=terraform output -json peer_ips \
    | python3 -c "import json,sys; print(json.load(sys.stdin)['$name'].rstrip('/32'))")"
  conf="$dir/$name.conf"
  cat > "$conf" <<EOF
[Interface]
PrivateKey = $(cat "$dir/private.key")
Address = $ip/32
DNS = 1.1.1.1, 1.0.0.1

[Peer]
PublicKey = $SERVER_PUB
Endpoint = EPHEMERAL_IP:$WG_PORT
AllowedIPs = 0.0.0.0/0, ::/0
PersistentKeepalive = 25
EOF
  chmod 600 "$conf"
  echo "wrote $conf — replace EPHEMERAL_IP with the IP the bot reports after /vpn start"
done
