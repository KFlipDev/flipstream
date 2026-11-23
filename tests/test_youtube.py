import pytest
from yt_dlp.networking.exceptions import HTTPError, TransportError
from yt_dlp.utils import DownloadError, ExtractorError

from yt2sc import youtube


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=abc",
        "https://youtu.be/abc",
        "https://music.youtube.com/watch?v=abc",
        "  https://m.youtube.com/watch?v=abc  ",
    ],
)
def test_youtube_urls_pass(url):
    assert youtube.check_url(url) == url.strip()


@pytest.mark.parametrize(
    "url",
    [
        "",
        "https://vimeo.com/123",
        "https://youtube.com.evil.example/watch?v=abc",
        "file:///etc/passwd",
        "ftp://youtube.com/x",
        "https://evil.example/?u=https://youtube.com/watch",
    ],
)
def test_other_urls_are_refused(url):
    with pytest.raises(youtube.LookupFailed):
        youtube.check_url(url)


def test_genre_from_tags_then_title():
    assert youtube.guess_genre(["sunset", "Deep House"], "Mix") == "Deep House"
    assert youtube.guess_genre([], "Boiler Room: techno set") == "Techno"
    assert youtube.guess_genre(["DnB"], "x") == "Drum and Bass"
    assert youtube.guess_genre(["housewarming"], "party") == ""


def info(**kw):
    base = {
        "id": "abc",
        "title": " Sunset Mix ",
        "uploader": "DJ K",
        "duration": 7471,
        "description": "two hours",
        "tags": ["house", "sunset"],
        "thumbnail": "https://i.ytimg.com/vi/abc/maxresdefault.jpg",
        "webpage_url": "https://www.youtube.com/watch?v=abc",
    }
    return base | kw


def test_video_info_prefill():
    v = youtube.to_video_info("u", info())
    assert (v.title, v.artist, v.genre, v.duration) == ("Sunset Mix", "DJ K", "House", 7471)
    assert v.thumbnail.startswith("https://")


@pytest.mark.parametrize(
    "extra",
    [
        {"_type": "playlist"},
        {"is_live": True},
        {"live_status": "is_upcoming"},
        {"duration": 7 * 3600},
    ],
    ids=["playlist", "live", "upcoming", "too-long"],
)
def test_video_info_refuses(extra):
    with pytest.raises(youtube.LookupFailed):
        youtube.to_video_info("u", info(**extra))


def test_insecure_thumbnail_dropped():
    assert youtube.to_video_info("u", info(thumbnail="http://x/y.jpg")).thumbnail == ""


class FakeResponse:
    def __init__(self, status):
        self.status = status
        self.reason = "x"
        self.headers = {}
        self.url = "https://example"

    def close(self):
        pass


def wrapped(exc):
    try:
        raise exc
    except Exception:
        import sys

        return DownloadError("ERROR: wrapped", sys.exc_info())


def test_network_errors_are_transient():
    assert youtube.is_transient(wrapped(TransportError("connection reset")))
    assert youtube.is_transient(wrapped(ExtractorError("x", cause=TransportError("timed out"))))
    assert youtube.is_transient(wrapped(ConnectionResetError()))
    assert youtube.is_transient(wrapped(HTTPError(FakeResponse(503))))


def test_blocks_and_missing_videos_are_not_transient():
    assert not youtube.is_transient(wrapped(HTTPError(FakeResponse(403))))
    assert not youtube.is_transient(wrapped(HTTPError(FakeResponse(429))))
    assert not youtube.is_transient(wrapped(ExtractorError("Video unavailable", expected=True)))
    assert not youtube.is_transient(DownloadError("ERROR: Sign in to confirm you're not a bot"))


def test_lookup_shows_yt_dlp_message(monkeypatch):
    class Boom:
        def __init__(self, opts):
            assert opts["skip_download"] is True

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download):
            assert download is False
            raise DownloadError("\x1b[0;31mERROR:\x1b[0m [youtube] abc: Video unavailable")

    monkeypatch.setattr(youtube.yt_dlp, "YoutubeDL", Boom)
    with pytest.raises(youtube.LookupFailed, match=r"^\[youtube\] abc: Video unavailable$"):
        youtube.lookup("https://youtu.be/abc")
