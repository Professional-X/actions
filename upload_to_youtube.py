#!/usr/bin/env python3
"""
upload_to_youtube.py

Downloads a video file from VIDEO_URL, then uploads it to the YouTube channel
connected to the configured Composio connected account.

The script uses Composio's proxy-execute endpoint to start a YouTube resumable
upload session (OAuth token is injected by Composio), then streams the file
bytes directly to YouTube's self-authorizing upload URL.

Environment variables (provided by the GitHub Actions workflow):
    COMPOSIO_API_KEY              Composio project API key
    COMPOSIO_CONNECTED_ACCOUNT_ID Connected account ID for the YouTube toolkit
    COMPOSIO_USER_ID              Composio user ID
    VIDEO_URL                     Direct URL to the video file to download
    INPUT_TITLE                   YouTube video title
    INPUT_DESCRIPTION             YouTube video description
    INPUT_PRIVACY                 public / unlisted / private (default: private)
    INPUT_CATEGORY_ID             YouTube category ID (default: 22 = People & Blogs)
    YOUTUBE_USER_ID               (optional) YouTube channel ID for verification

No additional setup required beyond the workflow secrets — just trigger and run.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import requests
from composio import Composio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def log(stage: str, msg: str) -> None:
    """Pretty stdout logger so each step is visible in the Actions log."""
    print(f"\n=== [{stage}] ===\n{msg}\n", flush=True)


def die(msg: str, code: int = 1) -> None:
    print(f"\n!! FATAL: {msg}", file=sys.stderr, flush=True)
    sys.exit(code)


def require_env(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        die(f"Missing required environment variable: {name}")
    return val


def parse_description(raw: str) -> str:
    """Workflow inputs can't contain real newlines, so allow `\\n` escapes."""
    return raw.replace("\\n", "\n").strip()


# ---------------------------------------------------------------------------
# Step 1 — download the video
# ---------------------------------------------------------------------------

def download_video(url: str, out_path: Path) -> int:
    log("download", f"Fetching {url}")
    with requests.get(url, stream=True, timeout=600) as r:
        r.raise_for_status()
        total = int(r.headers.get("Content-Length", "0"))
        written = 0
        with open(out_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                f.write(chunk)
                written += len(chunk)
                if total:
                    pct = written * 100 // total
                    print(f"\r  {written / 1e6:6.2f} MB / {total / 1e6:6.2f} MB  ({pct:3d}%)", end="", flush=True)
        print()
    size = out_path.stat().st_size
    log("download", f"Saved {size:,} bytes ({size / 1e6:.2f} MB) to {out_path}")
    return size


# ---------------------------------------------------------------------------
# Step 2 — initiate YouTube resumable upload via Composio proxy
# ---------------------------------------------------------------------------

def extract_location(proxy_response: Any) -> str:
    """Pull the upstream `Location` header out of a composio.tools.proxy() result.

    The SDK wraps the HTTP API response, which forwards upstream status/headers/data.
    The exact attribute name has shifted between SDK releases, so we try several shapes.
    """
    candidates: list[Any] = []

    # SDK object: try .headers / .response_headers / .data
    if hasattr(proxy_response, "headers"):
        candidates.append(getattr(proxy_response, "headers"))
    if hasattr(proxy_response, "response_headers"):
        candidates.append(getattr(proxy_response, "response_headers"))
    if hasattr(proxy_response, "data"):
        candidates.append(getattr(proxy_response, "data"))

    # Plain dict from the HTTP API
    if isinstance(proxy_response, dict):
        for key in ("headers", "response_headers", "responseHeaders"):
            if key in proxy_response:
                candidates.append(proxy_response[key])
        # Some SDK versions return the whole HTTP response as a dict
        if "data" in proxy_response and isinstance(proxy_response["data"], dict):
            for key in ("headers", "response_headers", "responseHeaders"):
                if key in proxy_response["data"]:
                    candidates.append(proxy_response["data"][key])

    for c in candidates:
        loc = _find_location_in(c)
        if loc:
            return loc

    raise RuntimeError(
        "Could not find 'Location' header in proxy response. "
        f"Response shape: {type(proxy_response).__name__} = {repr(proxy_response)[:1500]}"
    )


def _find_location_in(obj: Any) -> str | None:
    if obj is None:
        return None
    # dict-like
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str) and k.lower() == "location":
                return v if isinstance(v, str) else (v[0] if isinstance(v, list) and v else None)
        return None
    # list of {name, value} pairs (SDK parameter-style)
    if isinstance(obj, list):
        for item in obj:
            if isinstance(item, dict):
                name = item.get("name", "")
                if isinstance(name, str) and name.lower() == "location":
                    return item.get("value")
        return None
    return None


def initiate_resumable_upload(
    composio: Composio,
    *,
    connected_account_id: str,
    user_id: str,
    title: str,
    description: str,
    category_id: str,
    privacy: str,
    file_size: int,
) -> str:
    """Use Composio's proxy-execute to start a YouTube resumable upload session.

    Composio injects the OAuth bearer token on the server side. The response
    includes a `Location` header containing a self-authorizing upload URL we
    PUT the file bytes to directly.
    """
    body = {
        "snippet": {
            "title": title,
            "description": description,
            "categoryId": category_id,
            "defaultLanguage": "en",
            "defaultAudioLanguage": "en",
        },
        "status": {
            "privacyStatus": privacy,
            "embeddable": True,
            "selfDeclaredMadeForKids": False,
        },
    }

    params = [
        {"name": "X-Upload-Content-Type", "value": "video/mp4", "type": "header"},
        {"name": "X-Upload-Content-Length", "value": str(file_size), "type": "header"},
        {"name": "Content-Type", "value": "application/json; charset=UTF-8", "type": "header"},
        {"name": "Accept", "value": "application/json", "type": "header"},
    ]

    # Use an absolute URL — Composio's base URL for the YouTube toolkit is
    # `https://www.googleapis.com/youtube/v3`, so a relative path like
    # `/upload/youtube/v3/videos` would be appended to that and 404.
    # The resumable-upload endpoint lives at a different path prefix.
    endpoint = "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status"

    log("composio", f"Initiating resumable upload via proxy (size={file_size:,} bytes)")

    try:
        response = composio.tools.proxy(
            endpoint=endpoint,
            method="POST",
            connected_account_id=connected_account_id,
            user_id=user_id,
            body=json.dumps(body),
            parameters=params,
        )
    except Exception as exc:
        die(f"Composio proxy call failed: {exc!r}")

    log("composio", f"Proxy response type: {type(response).__name__}")

    # Surface a useful error if the upstream returned non-2xx.
    status = None
    if hasattr(response, "status"):
        status = getattr(response, "status")
    elif isinstance(response, dict):
        status = response.get("status") or response.get("statusCode")
    if status is not None and not (200 <= int(status) < 300):
        die(f"YouTube resumable-upload initiation failed with status {status}: {repr(response)[:1500]}")

    location = extract_location(response)
    log("composio", f"Got upload URL: {location[:100]}...")
    return location


# ---------------------------------------------------------------------------
# Step 3 — PUT the video bytes to the self-authorizing Location URL
# ---------------------------------------------------------------------------

def put_bytes(location: str, video_path: Path, file_size: int) -> dict:
    """Stream the file to YouTube's resumable-upload endpoint.

    YouTube's resumable protocol expects the raw octet stream in the body and
    the Content-Range header set for the whole file on the final PUT.
    """
    headers = {
        "Content-Type": "video/mp4",
        "Content-Length": str(file_size),
        # Final PUT — single chunk covering the whole file.
        "Content-Range": f"bytes 0-{file_size - 1}/{file_size}",
    }

    log("upload", f"PUT {file_size:,} bytes to YouTube")
    start = time.time()

    with open(video_path, "rb") as f:
        # Stream in 8 MiB chunks so we don't blow memory on large videos.
        resp = requests.put(location, headers=headers, data=f, timeout=7200)

    elapsed = time.time() - start
    log("upload", f"YouTube responded with HTTP {resp.status_code} in {elapsed:.1f}s")

    if resp.status_code >= 400:
        die(
            f"YouTube upload failed (HTTP {resp.status_code}):\n"
            f"Headers: {dict(resp.headers)}\n"
            f"Body: {resp.text[:2000]}"
        )

    try:
        return resp.json()
    except ValueError:
        # 308 Resume Incomplete or empty 200 body — return parsed headers instead.
        return {"_status_code": resp.status_code, "_headers": dict(resp.headers), "_body": resp.text}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    api_key = require_env("COMPOSIO_API_KEY")
    connected_account_id = require_env("COMPOSIO_CONNECTED_ACCOUNT_ID")
    user_id = require_env("COMPOSIO_USER_ID")
    video_url = require_env("VIDEO_URL")
    title = require_env("INPUT_TITLE")
    description = parse_description(os.environ.get("INPUT_DESCRIPTION", ""))
    privacy = os.environ.get("INPUT_PRIVACY", "private").strip().lower() or "private"
    category_id = os.environ.get("INPUT_CATEGORY_ID", "22").strip() or "22"

    if privacy not in {"public", "unlisted", "private"}:
        die(f"Invalid privacy_status: {privacy!r}")

    log("config", (
        f"Video URL   : {video_url}\n"
        f"Title       : {title}\n"
        f"Description : {description[:80]}{'...' if len(description) > 80 else ''}\n"
        f"Privacy     : {privacy}\n"
        f"Category ID : {category_id}\n"
        f"Connected   : {connected_account_id}\n"
        f"User ID     : {user_id}"
    ))

    composio = Composio(api_key=api_key)

    # 1. Download
    video_path = Path("video.mp4")
    file_size = download_video(video_url, video_path)

    # 2. Initiate resumable upload (gets us a self-authorizing Location URL)
    location = initiate_resumable_upload(
        composio,
        connected_account_id=connected_account_id,
        user_id=user_id,
        title=title,
        description=description,
        category_id=category_id,
        privacy=privacy,
        file_size=file_size,
    )

    # 3. Upload the bytes
    result = put_bytes(location, video_path, file_size)

    video_id = None
    if isinstance(result, dict):
        video_id = result.get("id")

    if video_id:
        log("done", f"YouTube video uploaded successfully!\n  ID: {video_id}\n  URL: https://www.youtube.com/watch?v={video_id}")
    else:
        log("done", f"YouTube upload returned non-standard response (may still be processing):\n{json.dumps(result, indent=2)[:1500]}")

    # Clean up the local file (CI runner will wipe it anyway, but be tidy).
    try:
        video_path.unlink(missing_ok=True)
    except Exception:
        pass


if __name__ == "__main__":
    main()
