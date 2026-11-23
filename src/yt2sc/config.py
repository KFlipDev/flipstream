"""Settings read from the environment (the compose env on the VM)."""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    version: str
    # Cloudflare Access: the application's AUD tag and the team domain,
    # for example "kflip.cloudflareaccess.com".
    access_aud: str
    team_domain: str
    # "access" (default) checks the Cf-Access JWT on every request but /healthz.
    # "off" skips it, for local development only.
    auth_mode: str

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            version=os.environ.get("YT2SC_VERSION", "dev"),
            access_aud=os.environ.get("CF_ACCESS_AUD", ""),
            team_domain=os.environ.get("CF_TEAM_DOMAIN", "").removeprefix("https://").rstrip("/"),
            auth_mode=os.environ.get("YT2SC_AUTH", "access"),
        )
