# actions

GitHub Actions workflow that downloads a video from a URL and uploads it to your YouTube channel — fully managed by Composio for OAuth. **No extra setup, just trigger and run.**

## How it works

1. A GitHub Actions workflow (`workflow_dispatch`) accepts **title**, **description**, privacy status, category ID, and an optional override video URL.
2. The runner downloads the video file from the configured URL.
3. The runner uses the **Composio SDK** (`tools.proxy`) to start a YouTube resumable-upload session — Composio injects the OAuth bearer token on its server side, so no YouTube client secret or refresh token lives in the repo.
4. The runner streams the file bytes directly to YouTube's self-authorizing upload URL.
5. The new video ID and `youtube.com/watch?v=…` link are printed in the Actions log.

## Prerequisites (one-time)

The following secrets must already be set in the repo (Settings → Secrets and variables → Actions):

| Secret | Purpose |
| --- | --- |
| `COMPOSIO_API_KEY` | Your Composio project API key |
| `COMPOSIO_CONNECTED_ACCOUNT_ID` | The connected account ID for the YouTube toolkit |
| `COMPOSIO_USER_ID` | The Composio user ID matching the connected account |
| `YOUTUBE_USER_ID` | (Optional) YouTube channel ID, for logging only |

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
upload_to_youtube.py                      # Download + resumable upload logic
requirements.txt                          # composio + requests
README.md                                 # This file
```

## Why proxy-execute instead of `YOUTUBE_UPLOAD_VIDEO`?

Composio exposes a `YOUTUBE_UPLOAD_VIDEO` action, but it has an open bug where uploaded videos are deleted by YouTube within ~90 seconds because the file body isn't streamed as raw octets. The reliable path is to use Composio's **proxy-execute** endpoint (`composio.tools.proxy(...)`) to initiate a YouTube resumable-upload session — Composio injects the OAuth credential — then PUT the bytes directly to YouTube's self-authorizing Location URL. That's what this repo does.

## Notes

- Default video URL baked into the workflow: the one provided when the repo was created. Override per-run with the `video_url` input.
- New YouTube uploads start in the privacy status you select — `private` is the safest default while you verify the upload.
- The Composio connected account must have the YouTube scopes (`youtube.upload`, `youtube.readonly`) already granted. If a new connection is needed, create one in the Composio dashboard.
- **Daily quota:** YouTube's `videos.insert` quota is enforced per OAuth client project (the `project_number:1068312718386` value in the error is Composio's shared YouTube OAuth client, not yours). When that project's daily quota is hit you'll see HTTP `429 RESOURCE_EXHAUSTED` from `youtube.googleapis.com`. The workflow retries up to 6 times with exponential backoff (30s → 60s → 120s → 240s → 480s → 960s) so transient rate-limit windows recover automatically, but if the *daily* quota is genuinely exhausted for the day, the run will eventually fail and you'll need to wait until the next day (or use a YouTube connection backed by your own OAuth client).
- The diagnostic workflow `.github/workflows/diag.yml` lists every connected account on the configured Composio user — run it from the Actions tab if you need to verify which `ca_...` ID belongs to which toolkit.

