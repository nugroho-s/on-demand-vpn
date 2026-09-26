#!/usr/bin/env bash
# Generate a WireGuard server keypair and N client keypairs.
# Server private key stays on disk for terraform.tfvars; client private keys
# never leave this machine — copy them into your client apps, then delete.
set -euo pipefail

cd "$(dirname "$0")/.."
mkdir -p server-key client-configs
umask 077

if [ ! -f server-key/private.key ]; then
  wg genkey | tee server-key/private.key | wg pubkey > server-key/public.key
  echo "server: server-key/private.key (put in terraform.tfvars wg_server_private_key)"
fi

count="${1:-1}"
for i in $(seq 1 "$count"); do
  name="peer-$(( $(ls client-configs 2>/dev/null | wc -l) + 1 ))"
  mkdir -p "client-configs/$name"
  wg genkey | tee "client-configs/$name/private.key" | wg pubkey > "client-configs/$name/public.key"
  echo "client $name: client-configs/$name/public.key (put in terraform.tfvars wg_peers)"
done

echo "Done. Remember: server-key/ and client-configs/ are gitignored."
