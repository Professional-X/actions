# actions

GitHub Actions workflow that downloads a video from a URL and uploads it to your YouTube channel — fully managed by Composio for OAuth. **No extra setup, just trigger and run.**

The workflow tries **two** Composio credential sets in order. If the primary fails (e.g. YouTube quota exhausted, OAuth state issue), it automatically falls through to the secondary credential set and retries the same upload with the same title/description. This means a single run can survive one credential set going down without losing the upload.

## How it works

1. A GitHub Actions workflow (`workflow_dispatch`) accepts **title**, **description**, privacy status, category ID, and an optional override video URL.
2. The runner downloads the video file from the configured URL **once** — the same file is reused for every credential-set attempt.
3. For each credential set (primary → secondary fallback), the runner:
   - Constructs a Composio SDK client with that set's API key.
   - Uses `tools.proxy()` to start a YouTube resumable-upload session — Composio injects the OAuth bearer token on its server side, so no YouTube client secret lives in the repo.
   - Streams the file bytes directly to YouTube's self-authorizing upload URL.
4. On quota (`429 RESOURCE_EXHAUSTED`) or any other hard error (`4xx`/`5xx`), it logs the failure and tries the next credential set.
5. The new video ID and `youtube.com/watch?v=…` link are printed in the Actions log.
6. Only if **all** credential sets fail does the run fail — and the log includes a per-set failure summary so you know exactly which set hit which error.

## Prerequisites (one-time)

The following secrets must be set in the repo (Settings → Secrets and variables → Actions):

| Secret | Required? | Purpose |
| --- | --- | --- |
| `COMPOSIO_API_KEY` | yes (primary) | Primary Composio project API key |
| `COMPOSIO_CONNECTED_ACCOUNT_ID` | yes (primary) | Primary connected account ID for the YouTube toolkit |
| `COMPOSIO_USER_ID` | yes (primary) | Primary Composio user ID |
| `COMPOSIO_API_KEY_2` | optional (fallback) | Secondary Composio project API key — used if primary fails |
| `COMPOSIO_CONNECTED_ACCOUNT_ID_2` | optional (fallback) | Secondary connected account ID for the YouTube toolkit |
| `COMPOSIO_USER_ID_2` | optional (fallback) | Secondary Composio user ID |
| `YOUTUBE_USER_ID` | optional | YouTube channel ID, for logging only |

If the secondary secrets aren't set, the workflow still works — it just has no fallback if the primary fails.

## Usage

### From the GitHub web UI

1. Open the repo on GitHub.
2. Click the **Actions** tab.
3. Select **Upload Video to YouTube** in the left sidebar.
4. Click **Run workflow** (top right).
5. Fill in the inputs:
   - `title` — required
   - `description` — use literal `\n` for new lines
   - `privacy_status` — `private` / `unlisted` / `public` (default: `private`)
   - `category_id` — YouTube category ID (default: `22` = People & Blogs)
   - `video_url` — optional; if left blank, the default video URL baked into the workflow is used.
6. Click **Run workflow**. Watch the live log for the YouTube video ID and URL.

### From the GitHub CLI

```bash
gh workflow run upload-to-youtube.yml \
  --repo <owner>/actions \
  -f title="My Composio upload" \
  -f description="Uploaded automatically from GitHub Actions\nvia Composio." \
  -f privacy_status=private \
  -f category_id=22
```

Watch the run:

```bash
gh run watch --repo <owner>/actions
```

## File layout

```
.github/workflows/upload-to-youtube.yml   # The workflow
.github/workflows/diag.yml                # Diagnostic: list all connected accounts
upload_to_youtube.py                      # Download + resumable upload + fallback logic
requirements.txt                          # composio + requests
README.md                                 # This file
```

## Why proxy-execute instead of `YOUTUBE_UPLOAD_VIDEO`?

Composio exposes a `YOUTUBE_UPLOAD_VIDEO` action, but it has an open bug where uploaded videos are deleted by YouTube within ~90 seconds because the file body isn't streamed as raw octets. The reliable path is to use Composio's **proxy-execute** endpoint (`composio.tools.proxy(...)`) to initiate a YouTube resumable-upload session — Composio injects the OAuth credential — then PUT the bytes directly to YouTube's self-authorizing Location URL. That's what this repo does.

## Notes

- Default video URL baked into the workflow: the one provided when the repo was created. Override per-run with the `video_url` input.
- New YouTube uploads start in the privacy status you select — `private` is the safest default while you verify the upload.
- Each Composio connected account must have the YouTube scopes (`youtube.upload`, `youtube.readonly`) already granted. If a new connection is needed, create one in the Composio dashboard.
- **Daily quota:** YouTube's `videos.insert` quota is enforced per OAuth client project (e.g. `project_number:1068312718386`). When that project's daily quota is hit you'll see HTTP `429 RESOURCE_EXHAUSTED` from `youtube.googleapis.com`. The workflow retries up to 6 times with exponential backoff (30s → 60s → 120s → 240s → 480s → 960s) so transient rate-limit windows recover automatically, then falls through to the secondary credential set if the primary quota is genuinely exhausted for the day.
- The diagnostic workflow `.github/workflows/diag.yml` lists every connected account on the configured Composio user — run it from the Actions tab if you need to verify which `ca_...` ID belongs to which toolkit.


