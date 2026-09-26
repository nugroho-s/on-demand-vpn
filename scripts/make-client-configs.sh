#!/usr/bin/env bash
# Render client .conf files from local keys + terraform outputs.
#
# Local peer directories are matched to terraform peers by PUBLIC KEY (not by
# name), so directory names like peer-1 work fine alongside tfvar names.
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

TF="${TF:-terraform}"
command -v "$TF" >/dev/null || TF=/tmp/opencode/bin/terraform
[ -f server-key/public.key ] || { echo "run scripts/gen-keys.sh first"; exit 1; }

SERVER_PUB="$(cat server-key/public.key)"
WG_PORT="$($TF -chdir=terraform output -raw wireguard_port)"
WG_MTU="$($TF -chdir=terraform output -raw wg_mtu 2>/dev/null || echo 1400)"
OUT=client-configs

# peer name -> public key, from terraform
declare -A PUBKEY_BY_IP=()
while IFS=$'\t' read -r name pubkey ip; do
  PUBKEY_BY_IP["$pubkey"]="$ip"
done < <($TF -chdir=terraform output -json peers | python3 -c '
import json, sys
for p in json.load(sys.stdin):
    print(p["name"], p["public_key"], p["ip"], sep="\t")
')

for dir in "$OUT"/*/; do
  name="$(basename "$dir")"
  [ -f "$dir/private.key" ] || continue
  pubkey="$(wg pubkey < "$dir/private.key")"
  ip="${PUBKEY_BY_IP[$pubkey]-}"
  if [ -z "$ip" ]; then
    echo "SKIP $name: its public key is not in terraform wg_peers (add it and re-apply)" >&2
    continue
  fi
  ip="${ip%/32}"
  conf="$dir/$name.conf"
  cat > "$conf" <<EOF
[Interface]
PrivateKey = $(cat "$dir/private.key")
Address = $ip/32
DNS = 1.1.1.1, 1.0.0.1
MTU = $WG_MTU

[Peer]
PublicKey = $SERVER_PUB
Endpoint = EPHEMERAL_IP:$WG_PORT
AllowedIPs = 0.0.0.0/0, ::/0
PersistentKeepalive = 25
EOF
  chmod 600 "$conf"
  echo "wrote $conf — replace EPHEMERAL_IP with the IP the bot reports after /vpn start"
done
