"""YouTube through yt-dlp: URL check, metadata-only lookup, error sorting.

The web container asks for metadata only (step 1 on the pipeline page). The
worker does its own fresh lookup when it downloads, because YouTube's signed
stream links expire.
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import yt_dlp
from yt_dlp.networking.exceptions import HTTPError, TransportError
from yt_dlp.utils import DownloadError, ExtractorError

log = logging.getLogger("flipstream.youtube")

YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
    "www.youtu.be",
}
MAX_DURATION = 6 * 3600  # seconds; a mix longer than this is probably a stream archive

# Matched against the video's tags, then its title, to prefill Genre. First hit
# wins, so the more specific names come first.
GENRES = [
    "deep house",
    "tech house",
    "progressive house",
    "afro house",
    "melodic techno",
    "drum and bass",
    "drum & bass",
    "dnb",
    "uk garage",
    "nu disco",
    "lo-fi",
    "hip hop",
    "house",
    "techno",
    "trance",
    "disco",
    "dubstep",
    "garage",
    "ambient",
    "downtempo",
    "breakbeat",
    "jungle",
    "electro",
    "funk",
    "soul",
    "jazz",
    "reggae",
    "r&b",
]
GENRE_LABELS = {"dnb": "Drum and Bass", "drum & bass": "Drum and Bass", "r&b": "R&B"}

ANSI = re.compile(r"\x1b\[[0-9;]*m")


class LookupFailed(Exception):
    """yt-dlp could not get the video; the message is shown to Kevin."""


class TransientError(Exception):
    """Worth a retry: network trouble or the share not answering."""


@dataclass
class VideoInfo:
    """What the review form shows about one video."""

    url: str
    video_id: str
    title: str
    artist: str
    duration: int
    description: str
    tags: list[str] = field(default_factory=list)
    genre: str = ""
    thumbnail: str = ""


class _Logger:
    """Sends yt-dlp's output to logging instead of stdout."""

    def debug(self, msg: str) -> None:
        if not msg.startswith("[debug] "):
            log.debug(msg)

    def info(self, msg: str) -> None:
        log.debug(msg)

    def warning(self, msg: str) -> None:
        log.warning(clean_message(msg))

    def error(self, msg: str) -> None:
        log.error(clean_message(msg))


BASE_OPTS: dict[str, Any] = {
    "logger": _Logger(),
    "noplaylist": True,
    "cachedir": False,
    "no_color": True,
    "noprogress": True,
    "socket_timeout": 30,
}


def check_url(url: str) -> str:
    """Return the trimmed URL if it points at YouTube, else raise LookupFailed."""
    url = url.strip()
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or (parts.hostname or "") not in YOUTUBE_HOSTS:
        raise LookupFailed("Paste a youtube.com or youtu.be link.")
    return url


def clean_message(msg: str) -> str:
    """Strip color codes and the "ERROR: " prefix from a yt-dlp message."""
    msg = ANSI.sub("", str(msg)).strip()
    return msg.removeprefix("ERROR: ").strip()


def is_transient(exc: BaseException) -> bool:
    """True for network errors worth a retry; False for blocks, missing videos and the rest.

    yt-dlp wraps the real error inside its own, sometimes several layers deep, so
    this walks through every wrapped error until it finds one it can judge.
    """
    seen: set[int] = set()
    todo: list[BaseException | None] = [exc]
    while todo:
        error = todo.pop()
        if error is None or id(error) in seen:
            continue
        seen.add(id(error))
        if isinstance(error, HTTPError):
            # 5xx is YouTube having a bad minute. 403 and 429 are a block: a retry
            # 30 seconds later meets the same block.
            return error.status >= 500
        if isinstance(error, TransportError | ConnectionError | TimeoutError):
            return True
        if isinstance(error, DownloadError) and error.exc_info:
            todo.append(error.exc_info[1])
        if isinstance(error, ExtractorError):
            todo.append(error.cause if isinstance(error.cause, BaseException) else None)
        todo.extend([error.__cause__, error.__context__])
    return False


def guess_genre(tags: list[str], title: str) -> str:
    """Return the first genre from GENRES found in the tags or the title, or ""."""
    searched_texts = [tag.lower() for tag in tags] + [title.lower()]
    for genre in GENRES:
        # Whole words only, so "house" does not match "housewarming".
        pattern = re.compile(rf"(?<![\w]){re.escape(genre)}(?![\w])")
        if any(pattern.search(text) for text in searched_texts):
            return GENRE_LABELS.get(genre, genre.title())
    return ""


def to_video_info(url: str, info: dict[str, Any]) -> VideoInfo:
    """Turn yt-dlp's info into a VideoInfo, refusing playlists, live videos and long ones."""
    if info.get("_type") == "playlist":
        raise LookupFailed("That's a playlist; paste the link of one video.")
    if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming"):
        raise LookupFailed("That video is live or hasn't started; try again once it has ended.")
    duration = int(info.get("duration") or 0)
    if duration > MAX_DURATION:
        raise LookupFailed(f"That video is {duration // 3600} hours long; the limit is 6.")
    tags = [tag for tag in (info.get("tags") or []) if isinstance(tag, str)][:10]
    title = (info.get("title") or info.get("id") or "Untitled").strip()
    thumb = info.get("thumbnail") or ""
    return VideoInfo(
        url=info.get("webpage_url") or url,
        video_id=info.get("id") or "",
        title=title,
        artist=(info.get("artist") or info.get("uploader") or info.get("channel") or "").strip(),
        duration=duration,
        description=(info.get("description") or "").strip(),
        tags=tags,
        genre=guess_genre(tags, title),
        thumbnail=thumb if thumb.startswith("https://") else "",
    )


def lookup(url: str) -> VideoInfo:
    """Metadata only, no download. Raises LookupFailed with yt-dlp's message."""
    url = check_url(url)
    opts = BASE_OPTS | {"skip_download": True, "quiet": True}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except DownloadError as exc:
        raise LookupFailed(clean_message(exc.msg or str(exc))) from exc
    if not info:
        raise LookupFailed("yt-dlp returned nothing for that link.")
    return to_video_info(url, info)
