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


class QuotaExceeded(Exception):
    """Raised when YouTube returns 429 RESOURCE_EXHAUSTED after all retries.

    Lets the caller (main) fall through to the next credential set instead
    of killing the whole process.
    """

    def __init__(self, credential_label: str, status: Any, response: Any) -> None:
        self.credential_label = credential_label
        self.status = status
        self.response = response
        super().__init__(
            f"YouTube quota exhausted for {credential_label} "
            f"(status={status}): {repr(response)[:600]}"
        )


class HardUploadFailure(Exception):
    """Raised when YouTube returns a non-2xx, non-429 error (auth, 4xx, 5xx).

    Falling through to the next credential set is still worth trying — the
    failure may be tied to a specific connection's OAuth state.
    """

    def __init__(self, credential_label: str, status: Any, response: Any) -> None:
        self.credential_label = credential_label
        self.status = status
        self.response = response
        super().__init__(
            f"YouTube upload initiation failed for {credential_label} "
            f"(status={status}): {repr(response)[:600]}"
        )


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
    credential_label: str,
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

    Raises:
        QuotaExceeded: YouTube returned 429 after all backoff retries.
            Caller should fall through to the next credential set.
        HardUploadFailure: YouTube returned any other non-2xx status.
            Caller should also fall through; the error may be tied to this
            specific connection's OAuth state.
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

    log("composio", f"[{credential_label}] Initiating resumable upload via proxy (size={file_size:,} bytes)")

    # YouTube may return 429 RESOURCE_EXHAUSTED on the project's daily video-upload
    # quota. Retry a few times with exponential backoff in case the limit is
    # enforced per-minute rather than truly per-day.
    max_attempts = 6
    base_delay = 30.0  # seconds
    response: Any = None
    last_status: Any = None
    for attempt in range(1, max_attempts + 1):
        try:
            # Per Composio v3.1 REST API: `body` is a nullable object (dict),
            # NOT a JSON string. Passing json.dumps(...) here causes YouTube to
            # see the body as a JSON-stringified string, which fails with
            # "Invalid JSON payload received. Unknown name '': Root element
            # must be a message."
            response = composio.tools.proxy(
                endpoint=endpoint,
                method="POST",
                connected_account_id=connected_account_id,
                user_id=user_id,
                body=body,
                parameters=params,
            )
        except Exception as exc:
            # Network/SDK-level error — try the next credential set.
            raise HardUploadFailure(credential_label, None, f"Composio proxy call raised: {exc!r}")

        status = None
        if hasattr(response, "status"):
            status = getattr(response, "status")
        elif isinstance(response, dict):
            status = response.get("status") or response.get("statusCode")
        last_status = status

        # Detect quota exhaustion: HTTP 429, or 200 with a RESOURCE_EXHAUSTED body.
        is_quota = False
        if status is not None and int(status) == 429:
            is_quota = True
        elif isinstance(response, dict) and isinstance(response.get("data"), dict):
            err = response["data"].get("error") or {}
            if err.get("status") == "RESOURCE_EXHAUSTED" or err.get("code") == 429:
                is_quota = True

        if is_quota and attempt < max_attempts:
            delay = base_delay * (2 ** (attempt - 1))
            log("composio", f"[{credential_label}] YouTube returned 429 quota (attempt {attempt}/{max_attempts}); retrying in {delay:.0f}s...")
            time.sleep(delay)
            continue

        if is_quota and attempt == max_attempts:
            # All retries exhausted — let caller fall through to next credential set.
            raise QuotaExceeded(credential_label, status, response)

        if status is not None and not (200 <= int(status) < 300):
            # Hard failure (400/401/403/404/5xx) — also let caller fall through;
            # the next credential set may have a healthier OAuth state.
            raise HardUploadFailure(credential_label, status, response)

        break  # success

    location = extract_location(response)
    log("composio", f"[{credential_label}] Got upload URL: {location[:100]}...")
    return location


# ---------------------------------------------------------------------------
# Step 3 — PUT the video bytes to the self-authorizing Location URL
# ---------------------------------------------------------------------------

def put_bytes(location: str, video_path: Path, file_size: int, credential_label: str) -> dict:
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

    log("upload", f"[{credential_label}] PUT {file_size:,} bytes to YouTube")
    start = time.time()

    with open(video_path, "rb") as f:
        # Stream in 8 MiB chunks so we don't blow memory on large videos.
        resp = requests.put(location, headers=headers, data=f, timeout=7200)

    elapsed = time.time() - start
    log("upload", f"[{credential_label}] YouTube responded with HTTP {resp.status_code} in {elapsed:.1f}s")

    if resp.status_code in (429, 403) or resp.status_code >= 500:
        # Transient failure that may be quota- or backend-related — let the
        # caller try the next credential set.
        raise HardUploadFailure(
            credential_label,
            resp.status_code,
            f"YouTube byte PUT failed (HTTP {resp.status_code}): {resp.text[:600]}",
        )

    if resp.status_code >= 400:
        # Hard 4xx — propagate as HardUploadFailure too; the next credential
        # set may be on a different OAuth state.
        raise HardUploadFailure(
            credential_label,
            resp.status_code,
            f"YouTube byte PUT failed (HTTP {resp.status_code}): {resp.text[:600]}",
        )

    try:
        return resp.json()
    except ValueError:
        # 308 Resume Incomplete or empty 200 body — return parsed headers instead.
        return {"_status_code": resp.status_code, "_headers": dict(resp.headers), "_body": resp.text}


# ---------------------------------------------------------------------------
# Credential set handling
# ---------------------------------------------------------------------------

def build_credential_sets() -> list[dict[str, str]]:
    """Build an ordered list of Composio credential sets to try.

    Always includes the primary set. Includes the secondary set only if its
    secrets are present in the environment (so older workflow runs that don't
    expose them still work).
    """
    sets: list[dict[str, str]] = []

    primary = {
        "label": "primary",
        "api_key": os.environ.get("COMPOSIO_API_KEY", "").strip(),
        "connected_account_id": os.environ.get("COMPOSIO_CONNECTED_ACCOUNT_ID", "").strip(),
        "user_id": os.environ.get("COMPOSIO_USER_ID", "").strip(),
    }
    if primary["api_key"] and primary["connected_account_id"] and primary["user_id"]:
        sets.append(primary)

    secondary = {
        "label": "secondary (fallback)",
        "api_key": os.environ.get("COMPOSIO_API_KEY_2", "").strip(),
        "connected_account_id": os.environ.get("COMPOSIO_CONNECTED_ACCOUNT_ID_2", "").strip(),
        "user_id": os.environ.get("COMPOSIO_USER_ID_2", "").strip(),
    }
    if secondary["api_key"] and secondary["connected_account_id"] and secondary["user_id"]:
        sets.append(secondary)

    if not sets:
        die("No Composio credential sets configured — set COMPOSIO_API_KEY/COMPOSIO_CONNECTED_ACCOUNT_ID/COMPOSIO_USER_ID (primary) at minimum.")
    return sets


def try_upload_with_credential_set(
    cred: dict[str, str],
    *,
    video_path: Path,
    file_size: int,
    title: str,
    description: str,
    category_id: str,
    privacy: str,
) -> dict:
    """Try a full upload cycle (initiate + PUT bytes) with one credential set.

    Raises QuotaExceeded or HardUploadFailure on failure; returns the YouTube
    video resource dict on success.
    """
    log("credential", (
        f"Trying credential set: {cred['label']}\n"
        f"  API key            : {cred['api_key'][:10]}...{cred['api_key'][-4:]}\n"
        f"  Connected account  : {cred['connected_account_id']}\n"
        f"  Composio user ID   : {cred['user_id']}"
    ))

    composio = Composio(api_key=cred["api_key"])

    # 1. Initiate resumable upload (gets us a self-authorizing Location URL).
    #    This is where most quota / auth failures surface.
    location = initiate_resumable_upload(
        composio,
        credential_label=cred["label"],
        connected_account_id=cred["connected_account_id"],
        user_id=cred["user_id"],
        title=title,
        description=description,
        category_id=category_id,
        privacy=privacy,
        file_size=file_size,
    )

    # 2. Upload the bytes. If YouTube rejects the byte PUT after a successful
    #    initiation (rare, but possible on mid-upload quota), propagate so the
    #    caller can fall through to the next credential set.
    return put_bytes(location, video_path, file_size, cred["label"])


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    video_url = require_env("VIDEO_URL")
    title = require_env("INPUT_TITLE")
    description = parse_description(os.environ.get("INPUT_DESCRIPTION", ""))
    privacy = os.environ.get("INPUT_PRIVACY", "private").strip().lower() or "private"
    category_id = os.environ.get("INPUT_CATEGORY_ID", "22").strip() or "22"

    if privacy not in {"public", "unlisted", "private"}:
        die(f"Invalid privacy_status: {privacy!r}")

    credential_sets = build_credential_sets()
    log("config", (
        f"Video URL   : {video_url}\n"
        f"Title       : {title}\n"
        f"Description : {description[:80]}{'...' if len(description) > 80 else ''}\n"
        f"Privacy     : {privacy}\n"
        f"Category ID : {category_id}\n"
        f"Credential sets to try (in order): {[c['label'] for c in credential_sets]}"
    ))

    # 1. Download the video ONCE — it's the same file for every credential set.
    video_path = Path("video.mp4")
    file_size = download_video(video_url, video_path)

    # 2. Try each credential set in order until one succeeds.
    failures: list[str] = []
    result: dict | None = None
    for cred in credential_sets:
        try:
            result = try_upload_with_credential_set(
                cred,
                video_path=video_path,
                file_size=file_size,
                title=title,
                description=description,
                category_id=category_id,
                privacy=privacy,
            )
            log("credential", f"[{cred['label']}] Upload succeeded ✓")
            break
        except QuotaExceeded as qe:
            log("credential", f"[{cred['label']}] Quota exhausted: {qe}")
            failures.append(f"[{cred['label']}] QUOTA_EXCEEDED: {qe}")
            continue
        except HardUploadFailure as hf:
            log("credential", f"[{cred['label']}] Hard failure: {hf}")
            failures.append(f"[{cred['label']}] HARD_FAILURE: {hf}")
            continue

    if result is None:
        # All credential sets failed. Print a tidy summary and exit.
        summary = "\n".join(failures)
        die(
            f"All {len(credential_sets)} credential set(s) failed. Summary:\n{summary}\n\n"
            "No video was uploaded. Re-run after fixing one of the credential sets "
            "or wait for quota to reset."
        )

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
