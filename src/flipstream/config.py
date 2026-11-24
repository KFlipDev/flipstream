"""Settings read from the environment (the compose env on the VM)."""

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    """Everything the web app and the worker read from the environment."""

    version: str
    # Cloudflare Access: the application's AUD tag and the team domain,
    # for example "kflip.cloudflareaccess.com".
    access_aud: str
    team_domain: str
    # "access" (default) checks the Cf-Access JWT on every request but /healthz.
    # "off" skips it, for local development only.
    auth_mode: str
    redis_url: str = "redis://localhost:6379/0"
    # The SMB share is mounted here (a CIFS volume in compose); MP3s go into
    # its share_subdir folder, which Spotify on the Mac reads as Local Files.
    share_root: Path = Path("/share")
    share_subdir: str = "flipstream"
    # Per-job working files, deleted after save or failure.
    scratch_dir: Path = Path("/scratch")

    @property
    def output_dir(self) -> Path:
        return self.share_root / self.share_subdir

    @classmethod
    def from_env(cls) -> "Settings":
        """Read each setting from its environment variable, with a default if unset."""
        return cls(
            version=os.environ.get("FLIPSTREAM_VERSION", "dev"),
            access_aud=os.environ.get("CF_ACCESS_AUD", ""),
            team_domain=os.environ.get("CF_TEAM_DOMAIN", "").removeprefix("https://").rstrip("/"),
            auth_mode=os.environ.get("FLIPSTREAM_AUTH", "access"),
            redis_url=os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
            share_root=Path(os.environ.get("FLIPSTREAM_SHARE_ROOT", "/share")),
            share_subdir=os.environ.get("FLIPSTREAM_SHARE_SUBDIR", "flipstream"),
            scratch_dir=Path(os.environ.get("FLIPSTREAM_SCRATCH", "/scratch")),
        )
