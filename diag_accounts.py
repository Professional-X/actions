#!/usr/bin/env python3
"""
Diagnostic: list all connected accounts for this Composio user, so we can find
the one that's actually connected to the YouTube toolkit.

Calls the raw Composio REST API directly (no SDK version assumptions) to list
connected accounts, then prints every account's id, toolkit slug, and status.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request


def main() -> None:
    api_key = os.environ["COMPOSIO_API_KEY"]
    user_id = os.environ.get("COMPOSIO_USER_ID", "")
    base = "https://backend.composio.dev/api/v3"

    # Try multiple list endpoints - SDK versions differ
    paths = [
        f"/connected_accounts?user_ids={user_id}&limit=200",
        f"/connected-accounts?user_ids={user_id}&limit=200",
        f"/connected_accounts?user_id={user_id}&limit=200",
        f"/connected-accounts?user_id={user_id}&limit=200",
        f"/connected_accounts?limit=200",
        f"/connected-accounts?limit=200",
    ]

    last_err = None
    for p in paths:
        url = base + p
        print(f"\nTrying GET {p}")
        req = urllib.request.Request(
            url,
            method="GET",
            headers={
                "x-api-key": api_key,
                "accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
                print(f"  -> HTTP {resp.status}")
                print(json.dumps(data, indent=2)[:5000])
                return
        except Exception as exc:
            last_err = exc
            print(f"  -> {type(exc).__name__}: {exc}")

    # If all GET paths failed, try POST to /api/v3/connected_accounts/get
    print("\nAll GET variants failed; trying POST /connected_accounts/get")
    url = base + "/connected_accounts/get"
    body = json.dumps({"user_ids": [user_id], "limit": 200}).encode()
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={
            "x-api-key": api_key,
            "accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
            print(f"  -> HTTP {resp.status}")
            print(json.dumps(data, indent=2)[:5000])
            return
    except Exception as exc:
        print(f"  -> {type(exc).__name__}: {exc}")

    print(f"\nAll attempts failed. Last error: {last_err}", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
