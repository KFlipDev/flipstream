"""ffmpeg steps and file names: square cover, MP3 V0 with tags, copy to the share."""

import os
import re
import subprocess
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

ProgressFn = Callable[[float], None]

# Characters Windows/SMB won't take in a name, plus control characters.
BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
MAX_NAME_BYTES = 200


class EncodeFailed(Exception):
    """ffmpeg exited non-zero; the message is the end of its output."""


@dataclass(frozen=True)
class Tags:
    """The ID3 tags written into the MP3."""

    title: str
    artist: str
    genre: str
    comment: str = ""
    year: str = ""


def mp3_name(title: str) -> str:
    """`<title>.mp3`, safe on the SMB share and in Finder."""
    name = unicodedata.normalize("NFC", title)
    name = BAD_CHARS.sub(" ", name)
    # No leading dot (hidden on the Mac) and no trailing dot or space (SMB drops them).
    name = re.sub(r"\s+", " ", name).lstrip(". ").rstrip(". ")
    while len(name.encode()) > MAX_NAME_BYTES:
        name = name[:-1].rstrip()
    return f"{name or 'untitled'}.mp3"


def _run(cmd: list[str], on_line: Callable[[str], None] | None = None) -> None:
    """Run ffmpeg and raise EncodeFailed if it exits with an error.

    on_line is called with each line ffmpeg prints, which is how progress is read.
    """
    proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
    )
    if proc.stdout is None or proc.stderr is None:
        raise EncodeFailed("ffmpeg: no output pipes")
    for line in proc.stdout:
        if on_line:
            on_line(line.strip())
    err = proc.stderr.read()
    if proc.wait() != 0:
        tail = " ".join(err.strip().splitlines()[-3:]) or f"exit code {proc.returncode}"
        raise EncodeFailed(f"ffmpeg: {tail}")


def square_cover(thumbnail: Path, out: Path) -> Path:
    """Center square crop of the thumbnail, as JPEG (what ID3 cover art readers expect)."""
    _run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(thumbnail),
            "-vf",
            "crop='min(iw,ih)':'min(iw,ih)'",
            "-frames:v",
            "1",
            "-q:v",
            "2",
            str(out),
        ]
    )
    return out


FFMPEG = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]


def encode_cmd(audio: Path, out: Path) -> list[str]:
    """Build the ffmpeg command that encodes the audio to an MP3, with no tags or cover.

    The cover is added afterwards by tag_cmd. With the cover in this same pass,
    ffmpeg 7 reports progress from the one-frame cover stream, so the progress
    stopped at the first 40 ms (DEV-53).
    """
    cmd = FFMPEG + ["-i", str(audio), "-map", "0:a:0", "-map_metadata", "-1"]
    # V0: LAME's best VBR setting, about 245 kbit/s.
    cmd += ["-c:a", "libmp3lame", "-q:a", "0"]
    cmd += ["-progress", "pipe:1", "-nostats", str(out)]
    return cmd


def tag_cmd(mp3: Path, cover: Path | None, tags: Tags, out: Path) -> list[str]:
    """Build the ffmpeg command that writes the tags and the cover into an MP3.

    The audio is copied as it is, not encoded again, so this takes seconds.
    """
    cmd = FFMPEG + ["-i", str(mp3)]
    if cover:
        cmd += ["-i", str(cover), "-map", "0:a:0", "-map", "1:v:0"]
        cmd += ["-disposition:v:0", "attached_pic"]
        cmd += ["-metadata:s:v", "title=Album cover", "-metadata:s:v", "comment=Cover (front)"]
    else:
        cmd += ["-map", "0:a:0"]
    cmd += ["-c", "copy", "-map_metadata", "-1"]
    # ID3v2.3: the version Spotify and macOS read most reliably.
    cmd += ["-id3v2_version", "3", "-write_id3v1", "1"]
    meta = {
        "title": tags.title,
        "artist": tags.artist,
        "album_artist": tags.artist,
        "album": tags.title,
        "genre": tags.genre,
        "comment": tags.comment,
        "date": tags.year,
    }
    for key, value in meta.items():
        if value:
            cmd += ["-metadata", f"{key}={value}"]
    cmd.append(str(out))
    return cmd


def encode_mp3(
    audio: Path,
    cover: Path | None,
    tags: Tags,
    out: Path,
    duration: float,
    progress: ProgressFn | None = None,
) -> Path:
    """Encode the audio to a tagged MP3 with its cover, and return the output path.

    Two ffmpeg passes: encode the audio (with progress), then add the tags and
    cover. progress is called with the fraction done, from 0.0 to 1.0.
    """

    def on_line(line: str) -> None:
        """Turn ffmpeg's "out_time_us=" progress lines into a fraction of the duration."""
        if progress and duration > 0 and line.startswith("out_time_us="):
            try:
                done = int(line.split("=", 1)[1]) / 1_000_000
            except ValueError:
                return
            progress(min(done / duration, 1.0))

    untagged = out.with_name(out.stem + ".untagged.mp3")
    _run(encode_cmd(audio, untagged), on_line)
    _run(tag_cmd(untagged, cover, tags, out))
    untagged.unlink()
    return out


def copy_with_progress(src: Path, dest: Path, progress: ProgressFn | None = None) -> Path:
    """Copy to `<dest>.part`, then rename, so Spotify never sees half a file."""
    total = src.stat().st_size or 1
    part = dest.with_name(dest.name + ".part")
    done = 0
    with src.open("rb") as fin, part.open("wb") as fout:
        while chunk := fin.read(4 * 1024 * 1024):
            fout.write(chunk)
            done += len(chunk)
            if progress:
                progress(done / total)
        fout.flush()
        os.fsync(fout.fileno())
    os.replace(part, dest)
    return dest
