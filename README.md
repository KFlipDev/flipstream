# yt2sc

Paste a YouTube DJ-mix URL, review the metadata, get an MP3 with its artwork and tags, saved for Spotify Local Files and offered as a download. A single-user app, self-hosted on a Proxmox VM in my homelab behind Cloudflare Tunnel and Cloudflare Access.

Status: v1.0 in progress. SoundCloud private upload is planned for v1.1.

## How it works

1. **Paste** a YouTube link. The web container asks yt-dlp for metadata only (nothing is downloaded) and shows a review form prefilled with the title, artist (channel), genre (guessed from the tags), tags, description and the artwork, a square crop of the thumbnail.
2. **Save MP3** puts a job on a Redis queue. One RQ worker runs one job at a time: `queued → downloading → encoding → saving → done | failed`. The job page polls with htmx.
3. The worker looks the video up again (stream links expire), downloads the best audio with yt-dlp, and encodes an MP3 with ffmpeg: LAME V0 VBR, ID3v2.3 title, artist, album, genre, comment and the cover.
4. It saves `<title>.mp3` to `yt2sc/` on a Samba share (a CIFS volume in compose). Spotify desktop on the Mac reads that folder as Local Files. The done page also offers **Download MP3**, served from the share copy. Scratch files are deleted after save or failure.

Failures: network errors and an unreachable share are retried by RQ up to 3 times (30 s, 2 min, 5 min apart). A YouTube block, a missing video or an ffmpeg error fails at once with its message.

The image installs the newest yt-dlp at build time; YouTube changes often, so a stale yt-dlp is the most likely cause of a failed download.

## How it ships

- **Pull request:** `ruff`, `pytest` and `docker build` on GitHub-hosted runners. All three must pass before a merge; push protection blocks commits that contain secrets.
- **Merge to `main`:** the image is built and pushed to `ghcr.io/kflipdev/yt2sc` as `:sha-<commit>` and `:main`.
- **Deploy:** a self-hosted runner on the VM (outbound only, no open ports) runs [`deploy/yt2sc-deploy`](deploy/yt2sc-deploy): secrets from Bitwarden Secrets Manager into the compose env, `docker compose pull` and `up -d`, then `GET /healthz` must report the new version. If it doesn't, the previous `sha-` tag goes back up and the job fails. A deploy waits for a running job: the worker finishes it before it stops (RQ warm shutdown, `stop_grace_period` in compose).
- **Sign-in:** Cloudflare Access in front, and the app checks the `Cf-Access-Jwt-Assertion` JWT itself (signature, audience, issuer, expiry), so a request that skips Access is refused.

## Run it locally

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest -q
YT2SC_AUTH=off uvicorn yt2sc.app:app --reload
```

The pipeline needs Redis, ffmpeg and deno (yt-dlp's JavaScript runtime for YouTube); the image has them. The media tests skip without ffmpeg. `YT2SC_AUTH=off` turns the Access check off for local development. Without it and without `CF_ACCESS_AUD` and `CF_TEAM_DOMAIN`, every page but `/healthz` answers 503.
