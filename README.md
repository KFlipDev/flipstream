# yt2sc

Paste a YouTube DJ-mix URL, review the metadata, get an MP3 with its artwork and tags, saved for Spotify Local Files and offered as a download. A single-user app, self-hosted on a Proxmox VM in my homelab behind Cloudflare Tunnel and Cloudflare Access.

Status: v1.0 in progress. Live today: the health page, deployed from `main`. Next: the mix-to-MP3 pipeline (FastAPI, Redis, RQ worker, yt-dlp, ffmpeg).

## How it ships

- **Pull request:** `ruff`, `pytest` and `docker build` on GitHub-hosted runners. All three must pass before a merge; push protection blocks commits that contain secrets.
- **Merge to `main`:** the image is built and pushed to `ghcr.io/kflipdev/yt2sc` as `:sha-<commit>` and `:main`.
- **Deploy:** a self-hosted runner on the VM (outbound only, no open ports) runs [`deploy/yt2sc-deploy`](deploy/yt2sc-deploy): secrets from Bitwarden Secrets Manager into the compose env, `docker compose pull` and `up -d`, then `GET /healthz` must report the new version. If it doesn't, the previous `sha-` tag goes back up and the job fails.
- **Sign-in:** Cloudflare Access in front, and the app checks the `Cf-Access-Jwt-Assertion` JWT itself (signature, audience, issuer, expiry), so a request that skips Access is refused.

## Run it locally

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest -q
YT2SC_AUTH=off uvicorn yt2sc.app:app --reload
```

`YT2SC_AUTH=off` turns the Access check off for local development. Without it and without `CF_ACCESS_AUD` and `CF_TEAM_DOMAIN`, every page but `/healthz` answers 503.
