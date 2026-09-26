"""On-demand VPN Discord bot.

Runs on Cloud Run (Flask + gunicorn). Handles Discord interaction webhooks
(signature-verified) and Pub/Sub push messages from Cloud Scheduler. The VM is
started/stopped via the GCE API; because the VM has an ephemeral public IP,
the bot always reports the current endpoint after a start.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time
import urllib.request

import nacl.signing
from flask import Flask, abort, jsonify, request
from google.cloud import compute_v1

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("vpn-bot")

app = Flask(__name__)

PROJECT = os.environ["GCP_PROJECT"]
ZONE = os.environ["GCP_ZONE"]
INSTANCE = os.environ["INSTANCE_NAME"]
PUBLIC_KEY = os.environ["DISCORD_PUBLIC_KEY"]
WG_PORT = os.environ.get("WG_PORT", "51820")
BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
ALLOWED_USER_IDS = {
    uid.strip() for uid in os.environ.get("ALLOWED_USER_IDS", "").split(",") if uid.strip()
}

verifier = nacl.signing.VerifyKey(bytes.fromhex(PUBLIC_KEY))

_instances_client: compute_v1.InstancesClient | None = None


def client() -> compute_v1.InstancesClient:
    global _instances_client
    if _instances_client is None:
        _instances_client = compute_v1.InstancesClient()
    return _instances_client


def get_instance() -> compute_v1.Instance:
    return client().get(project=PROJECT, zone=ZONE, instance=INSTANCE)


def vm_status(instance: compute_v1.Instance) -> tuple[str, str | None]:
    """Return (status, external_ip)."""
    status = instance.status  # RUNNING, TERMINATED, STOPPING, PROVISIONING, ...
    ip = None
    for iface in instance.network_interfaces:
        for cfg in iface.access_configs:
            if cfg.nat_i_p:
                ip = cfg.nat_i_p
                break
        if ip:
            break
    return status, ip


def start_vm() -> None:
    if get_instance().status == "RUNNING":
        return
    client().start(project=PROJECT, zone=ZONE, instance=INSTANCE)
    for _ in range(120):
        time.sleep(2)
        if get_instance().status == "RUNNING":
            return
    raise TimeoutError("VM did not reach RUNNING within 240s")


def stop_vm() -> None:
    if get_instance().status in ("TERMINATED", "STOPPING"):
        return
    client().stop(project=PROJECT, zone=ZONE, instance=INSTANCE)
    for _ in range(60):
        time.sleep(2)
        if get_instance().status == "TERMINATED":
            return
    # stop is eventually consistent even if we time out polling


def handle_vpn_action(action: str | None, user_id: str, token: str = "") -> dict:
    if action == "status":
        status, ip = vm_status(get_instance())
        if status == "RUNNING":
            content = f"🟢 VPN is **RUNNING**\nEndpoint: `{ip}:{WG_PORT}`"
        elif status == "TERMINATED":
            content = "⚪ VPN is **STOPPED** (no compute cost while stopped)"
        else:
            content = f"🟡 VPN is **{status}**"
        return {"content": content}

    if action == "start":
        threading.Thread(target=_start_and_patch, args=(token,), daemon=True).start()
        return {
            "content": "🚀 Starting VPN… (boot takes ~30-60s, I'll update this message with the endpoint)",
        }

    if action == "stop":
        stop_vm()
        return {"content": "🛑 VPN stopped. Only disk storage is billed (~$0.40/mo)."}

    return {"content": f"Unknown action: {action}"}


@app.post("/interactions")
def interactions():
    signature = request.headers.get("X-Signature-Ed25519", "")
    timestamp = request.headers.get("X-Signature-Timestamp", "")
    body = request.get_data()

    try:
        verifier.verify(timestamp.encode() + body, bytes.fromhex(signature))
    except Exception:
        log.warning("Invalid interaction signature")
        abort(401)

    payload = json.loads(body)

    if payload["type"] == 1:  # PING from Discord
        return jsonify({"type": 1})

    # type 2 = APPLICATION_COMMAND; type 3 = MESSAGE_COMPONENT etc.
    if payload["type"] != 2:
        return jsonify({"type": 4, "data": {"content": "Unsupported interaction."}})

    data = payload["data"]
    user = payload.get("member", {}).get("user") or payload.get("user", {})
    user_id = user.get("id", "")
    interaction_token = payload.get("token", "")

    if ALLOWED_USER_IDS and user_id not in ALLOWED_USER_IDS:
        return jsonify({"type": 4, "data": {"content": "⛔ Not allowed."}})

    action = next(
        (o["value"] for o in data.get("options", []) if o["name"] == "action"),
        None,
    )
    result = handle_vpn_action(action, user_id, interaction_token)
    # type 4 = CHANNEL_MESSAGE_WITH_SOURCE (sent within Discord's 3s window)
    return jsonify({"type": 4, "data": result})


def _start_and_patch(token: str) -> None:
    """Start the VM (slow), then PATCH the original interaction response."""
    try:
        start_vm()
        _, ip = vm_status(get_instance())
        content = (
            f"🚀 VPN started.\nEndpoint: `{ip}:{WG_PORT}`\n"
            "⚠️ Ephemeral IP — update your client config endpoint if it changed."
        )
    except Exception:
        log.exception("start failed")
        content = "❌ Failed to start the VPN — check Cloud Run logs."
    _patch_original(token, content)


def _patch_original(token: str, content: str) -> None:
    if not token or not BOT_TOKEN:
        log.info("no token; would post: %s", content)
        return
    app_id = os.environ.get("DISCORD_APP_ID")
    if not app_id:
        log.warning("DISCORD_APP_ID not set; cannot patch response")
        return
    try:
        req = urllib.request.Request(
            f"https://discord.com/api/v10/webhooks/{app_id}/{token}/messages/@original",
            data=json.dumps({"content": content}).encode(),
            headers={
                "Authorization": f"Bot {BOT_TOKEN}",
                "Content-Type": "application/json",
            },
            method="PATCH",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            log.info("patched original response: %s", resp.status)
    except Exception:
        log.exception("failed to patch original response")


@app.post("/pubsub/stop")
def pubsub_stop():
    """Pub/Sub push endpoint (invoked by the nightly Cloud Scheduler job)."""
    body = request.get_json(force=True)
    msg = base64.b64decode(body["message"]["data"]).decode()
    log.info("scheduler stop requested: %s", msg)
    stop_vm()
    return "", 204


@app.get("/healthz")
def healthz():
    return "ok"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
