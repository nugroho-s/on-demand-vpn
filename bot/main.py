"""On-demand VPN Discord bot.

Runs on Cloud Run (Flask + gunicorn). Handles Discord interaction webhooks
(signature-verified) and Pub/Sub push messages from Cloud Scheduler.
VM is created from an Instance Template on-demand and deleted on stop/idle/region switch.
Zero storage or compute cost when stopped.
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
INSTANCE = os.environ.get("INSTANCE_NAME", "on-demand-vpn")
INSTANCE_TEMPLATE = os.environ.get("INSTANCE_TEMPLATE", "")
DEFAULT_LOCATION = os.environ.get("DEFAULT_LOCATION", "sg")
LOCATIONS_RAW = os.environ.get("LOCATIONS", "{}")
LOCATIONS: dict[str, dict[str, str]] = json.loads(LOCATIONS_RAW) if LOCATIONS_RAW else {}

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


def find_active_vm() -> tuple[compute_v1.Instance | None, str | None]:
    """Find the VPN instance across all zones in the project. Returns (instance, zone)."""
    try:
        for _, scoped_list in client().aggregated_list(project=PROJECT):
            if scoped_list.instances:
                for inst in scoped_list.instances:
                    if inst.name == INSTANCE:
                        zone = inst.zone.split("/")[-1]
                        return inst, zone
    except Exception:
        log.exception("failed to search for active vm")
    return None, None


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


def delete_vm(zone: str) -> None:
    """Delete VM and attached auto-delete boot disk."""
    log.info("Deleting instance %s in zone %s", INSTANCE, zone)
    try:
        client().delete(project=PROJECT, zone=zone, instance=INSTANCE)
    except Exception as e:
        log.exception("Error calling delete instance: %s", e)
        return

    for _ in range(60):
        time.sleep(2)
        inst, _ = find_active_vm()
        if not inst:
            log.info("Instance %s in zone %s successfully deleted", INSTANCE, zone)
            return
    log.warning("Timed out waiting for instance %s deletion", INSTANCE)


def create_vm(zone: str, subnetwork_url: str) -> compute_v1.Instance:
    """Create VM from instance template with target regional subnet."""
    log.info("Creating instance %s in zone %s from template %s", INSTANCE, zone, INSTANCE_TEMPLATE)
    instance_resource = compute_v1.Instance(
        name=INSTANCE,
        network_interfaces=[
            compute_v1.NetworkInterface(
                subnetwork=subnetwork_url,
                access_configs=[
                    compute_v1.AccessConfig(
                        name="External NAT",
                        type_="ONE_TO_ONE_NAT",
                    )
                ],
            )
        ],
    )
    req = compute_v1.InsertInstanceRequest(
        project=PROJECT,
        zone=zone,
        source_instance_template=INSTANCE_TEMPLATE,
        instance_resource=instance_resource,
    )
    client().insert(request=req)
    for _ in range(60):
        time.sleep(2)
        try:
            inst = client().get(project=PROJECT, zone=zone, instance=INSTANCE)
            if inst.status == "RUNNING":
                return inst
        except Exception:
            pass
    raise TimeoutError(f"VM {INSTANCE} did not reach RUNNING within 120s")


def handle_vpn_action(action: str | None, location: str | None, user_id: str, token: str = "") -> dict:
    if action == "status":
        inst, zone = find_active_vm()
        if not inst:
            return {"content": "⚪ VPN is **STOPPED** (0 active resources, $0/hr)"}

        loc_alias = next((k for k, v in LOCATIONS.items() if v.get("zone") == zone), zone or "unknown")
        status, ip = vm_status(inst)
        if status == "RUNNING":
            content = f"🟢 VPN is **RUNNING** in **{loc_alias.upper()}** (`{zone}`)\nEndpoint: `{ip}:{WG_PORT}`"
        else:
            content = f"🟡 VPN is **{status}** in **{loc_alias.upper()}** (`{zone}`)"
        return {"content": content}

    if action == "start":
        target_loc = (location or DEFAULT_LOCATION).lower()
        if target_loc not in LOCATIONS:
            valid = ", ".join(f"`{k}`" for k in LOCATIONS.keys())
            return {"content": f"❌ Unknown location `{target_loc}`. Choose from: {valid}"}

        threading.Thread(target=_start_and_patch, args=(token, target_loc), daemon=True).start()
        return {
            "content": f"🚀 Starting VPN in **{target_loc.upper()}**… (provisioning takes ~45-60s, I'll update this message with the endpoint)",
        }

    if action == "stop":
        threading.Thread(target=_stop_and_patch, args=(token,), daemon=True).start()
        return {"content": "🛑 Stopping VPN and deleting VM resources… (I'll update this message once cleaned up)"}

    return {"content": f"Unknown action: {action}"}


def _start_and_patch(token: str, location: str) -> None:
    try:
        loc_cfg = LOCATIONS[location]
        target_zone = loc_cfg["zone"]
        target_subnet = loc_cfg["subnetwork"]

        existing_inst, existing_zone = find_active_vm()
        if existing_inst and existing_zone:
            if existing_zone == target_zone and existing_inst.status == "RUNNING":
                inst = existing_inst
            else:
                log.info("Existing VM in %s must be deleted before starting in %s", existing_zone, target_zone)
                delete_vm(existing_zone)
                inst = create_vm(target_zone, target_subnet)
        else:
            inst = create_vm(target_zone, target_subnet)

        _, ip = vm_status(inst)
        content = (
            f"🚀 VPN started in **{location.upper()}** (`{target_zone}`).\n"
            f"Endpoint: `{ip}:{WG_PORT}`\n"
            "⚠️ Ephemeral IP — update your client config endpoint if it changed."
        )
    except Exception:
        log.exception("start failed")
        content = "❌ Failed to start the VPN — check Cloud Run logs."
    _patch_original(token, content)


def _stop_and_patch(token: str) -> None:
    try:
        inst, zone = find_active_vm()
        if inst and zone:
            delete_vm(zone)
            content = "🛑 VPN stopped. VM and disk deleted (0 compute and 0 storage cost)."
        else:
            content = "⚪ VPN is already stopped (no active VM found)."
    except Exception:
        log.exception("stop failed")
        content = "❌ Failed to stop the VPN — check Cloud Run logs."
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
                "User-Agent": "on-demand-vpn-bot (https://github.com/nugroho-s/on-demand-vpn, 1.1)",
            },
            method="PATCH",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            log.info("patched original response: %s", resp.status)
    except Exception:
        log.exception("failed to patch original response")


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
    location = next(
        (o["value"] for o in data.get("options", []) if o["name"] == "location"),
        None,
    )
    result = handle_vpn_action(action, location, user_id, interaction_token)
    # type 4 = CHANNEL_MESSAGE_WITH_SOURCE (sent within Discord's 3s window)
    return jsonify({"type": 4, "data": result})


@app.post("/pubsub/stop")
def pubsub_stop():
    """Pub/Sub push endpoint (invoked by the nightly Cloud Scheduler job)."""
    body = request.get_json(force=True)
    msg = base64.b64decode(body["message"]["data"]).decode()
    log.info("scheduler stop requested: %s", msg)
    inst, zone = find_active_vm()
    if inst and zone:
        delete_vm(zone)
    return "", 204


@app.get("/healthz")
def healthz():
    return "ok"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
