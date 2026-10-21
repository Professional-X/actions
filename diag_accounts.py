#!/usr/bin/env python3
"""
Diagnostic: list all connected accounts for this Composio user, so we can find
the one that's actually connected to the YouTube toolkit.

Prints every connected account's id, toolkit name, status, and auth scheme,
so we can pick the right `ca_...` value to use as COMPOSIO_CONNECTED_ACCOUNT_ID.
"""
from __future__ import annotations

import os

from composio import Composio


def main() -> None:
    api_key = os.environ["COMPOSIO_API_KEY"]
    composio = Composio(api_key=api_key)

    # The SDK exposes connected_accounts.list()
    accounts = composio.connected_accounts.get(user_id=os.environ.get("COMPOSIO_USER_ID"))
    # `accounts` may be a list or a paginated response; handle both.
    items = accounts if isinstance(accounts, list) else getattr(accounts, "items", None) or getattr(accounts, "data", None) or []
    print(f"\nTotal connected accounts: {len(items)}\n")
    for a in items:
        # Each `a` may be a dict or a typed object — try both shapes.
        if isinstance(a, dict):
            ca_id = a.get("id")
            toolkit = (a.get("toolkit") or {})
            toolkit_slug = toolkit.get("slug") if isinstance(toolkit, dict) else toolkit
            status = a.get("status")
            auth_scheme = a.get("auth_scheme") or a.get("authScheme")
            client_id = a.get("client_id") or a.get("clientId")
        else:
            ca_id = getattr(a, "id", None)
            toolkit = getattr(a, "toolkit", None)
            toolkit_slug = getattr(toolkit, "slug", toolkit)
            status = getattr(a, "status", None)
            auth_scheme = getattr(a, "auth_scheme", getattr(a, "authScheme", None))
            client_id = getattr(a, "client_id", getattr(a, "clientId", None))
        flag = "  <-- YOUTUBE" if str(toolkit_slug).upper().startswith("YOUTUBE") else ""
        print(f"id={ca_id}  toolkit={toolkit_slug}  status={status}  auth_scheme={auth_scheme}  client_id={client_id}{flag}")


if __name__ == "__main__":
    main()
