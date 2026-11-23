import time

import fakeredis
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from yt2sc.app import create_app
from yt2sc.auth import HEADER, AccessVerifier
from yt2sc.config import Settings

TEAM = "example.cloudflareaccess.com"
AUD = "test-aud-tag"


@pytest.fixture(scope="module")
def keys():
    good = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return good, other


def settings(**overrides) -> Settings:
    base = dict(version="sha-test123", access_aud=AUD, team_domain=TEAM, auth_mode="access")
    return Settings(**(base | overrides))


def token(key, **claims) -> str:
    now = int(time.time())
    body = {"iss": f"https://{TEAM}", "aud": [AUD], "email": "kevin@example.com"}
    body |= {"iat": now, "exp": now + 300} | claims
    return jwt.encode(body, key, algorithm="RS256")


@pytest.fixture
def client(keys):
    good, _ = keys
    verifier = AccessVerifier(TEAM, AUD, key_resolver=lambda _t: good.public_key())
    return TestClient(create_app(settings(), verifier, conn=fakeredis.FakeRedis()))


def test_healthz_is_open_and_reports_version(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["version"] == "sha-test123"
    assert body["redis"] == "ok"
    assert body["yt_dlp"]


def test_home_without_token_is_forbidden(client):
    assert client.get("/").status_code == 403


def test_home_with_valid_token_shows_email_and_version(client, keys):
    r = client.get("/", headers={HEADER: token(keys[0])})
    assert r.status_code == 200
    assert "kevin@example.com" in r.text
    assert "sha-test123" in r.text


@pytest.mark.parametrize(
    "claims",
    [
        {"aud": ["some-other-app"]},
        {"iss": "https://evil.cloudflareaccess.com"},
        {"exp": int(time.time()) - 60},
    ],
    ids=["wrong-aud", "wrong-issuer", "expired"],
)
def test_bad_claims_are_forbidden(client, keys, claims):
    assert client.get("/", headers={HEADER: token(keys[0], **claims)}).status_code == 403


def test_token_signed_by_another_key_is_forbidden(client, keys):
    assert client.get("/", headers={HEADER: token(keys[1])}).status_code == 403


def test_hs256_token_is_forbidden(client):
    forged = jwt.encode(
        {"iss": f"https://{TEAM}", "aud": AUD, "exp": int(time.time()) + 60, "iat": 0},
        "secret-" * 6,
        algorithm="HS256",
    )
    assert client.get("/", headers={HEADER: forged}).status_code == 403


def test_missing_access_settings_fail_closed():
    c = TestClient(create_app(settings(access_aud="", team_domain=""), conn=fakeredis.FakeRedis()))
    assert c.get("/").status_code == 503
    assert c.get("/healthz").status_code == 200


def test_auth_off_for_local_development():
    c = TestClient(create_app(settings(auth_mode="off"), conn=fakeredis.FakeRedis()))
    r = c.get("/")
    assert r.status_code == 200
    assert "local development" in r.text


def test_verifier_needs_both_settings():
    with pytest.raises(ValueError):
        AccessVerifier("", AUD)
