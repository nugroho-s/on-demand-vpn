"""One-off script to register the /vpn slash command globally with Discord.

Run locally:
    python register_commands.py

Requires DISCORD_BOT_TOKEN (and optional DISCORD_APP_ID) in the environment.
"""

import json
import os
import sys
import urllib.error
import urllib.request

TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
APP_ID = os.environ.get("DISCORD_APP_ID")

COMMANDS = [
    {
        "name": "vpn",
        "description": "Control the on-demand VPN",
        "options": [
            {
                "name": "action",
                "description": "What to do",
                "type": 3,  # STRING
                "required": True,
                "choices": [
                    {"name": "status", "value": "status"},
                    {"name": "start", "value": "start"},
                    {"name": "stop", "value": "stop"},
                ],
            }
        ],
    }
]


def main() -> None:
    if not TOKEN or not APP_ID:
        sys.exit("Set DISCORD_BOT_TOKEN and DISCORD_APP_ID first")

    url = f"https://discord.com/api/v10/applications/{APP_ID}/commands"
    req = urllib.request.Request(
        url,
        data=json.dumps(COMMANDS).encode(),
        headers={
            "Authorization": f"Bot {TOKEN}",
            "Content-Type": "application/json",
            # Discord's Cloudflare blocks the default python-urllib User-Agent (error 1010)
            "User-Agent": "on-demand-vpn-setup (https://github.com/nugsky/on-demand-vpn, 1.0)",
        },
        method="PUT",
    )
    with urllib.request.urlopen(req) as resp:
        print(resp.status, resp.read().decode())


if __name__ == "__main__":
    try:
        main()
    except urllib.error.HTTPError as e:
        sys.exit(f"Discord API error {e.code}: {e.read().decode()}")
