import json
import subprocess

import pytest

from flipstream import media

from .conftest import needs_ffmpeg


@pytest.mark.parametrize(
    "title,name",
    [
        ("Sunset House Mix · November 2026", "Sunset House Mix · November 2026.mp3"),
        ('AC/DC: "Live" <2026>?', "AC DC Live 2026.mp3"),
        ("../../etc/passwd", "etc passwd.mp3"),
        ("  ...  ", "untitled.mp3"),
        ("tab\tand\nnewline", "tab and newline.mp3"),
    ],
)
def test_mp3_name(title, name):
    assert media.mp3_name(title) == name


def test_mp3_name_length_capped():
    name = media.mp3_name("ü" * 300)
    assert len(name.encode()) <= media.MAX_NAME_BYTES + 4


def test_encode_cmd_is_v0_audio_only(tmp_path):
    cmd = media.encode_cmd(tmp_path / "a.webm", tmp_path / "o.mp3")
    joined = " ".join(cmd)
    assert "-c:a libmp3lame -q:a 0" in joined
    assert "-progress pipe:1" in joined
    # No cover in this pass: it would take over ffmpeg's progress (DEV-53).
    assert "-map 0:a:0" in joined and "1:v:0" not in joined


def test_tag_cmd_copies_audio_and_adds_cover_and_tags(tmp_path):
    tags = media.Tags(title="T", artist="A", genre="House", comment="C", year="2026")
    cmd = media.tag_cmd(tmp_path / "a.mp3", tmp_path / "c.jpg", tags, tmp_path / "o.mp3")
    joined = " ".join(cmd)
    assert "-c copy" in joined
    assert "-disposition:v:0 attached_pic" in joined
    assert "-id3v2_version 3" in joined
    for metadata in ("title=T", "artist=A", "genre=House", "comment=C", "date=2026"):
        assert metadata in cmd


def test_copy_with_progress_renames_at_the_end(tmp_path):
    src = tmp_path / "src.mp3"
    src.write_bytes(b"x" * (9 * 1024 * 1024))
    seen = []
    out = media.copy_with_progress(src, tmp_path / "dest.mp3", seen.append)
    assert out.read_bytes() == src.read_bytes()
    assert not (tmp_path / "dest.mp3.part").exists()
    assert seen[-1] == 1.0 and len(seen) == 3


@needs_ffmpeg
def test_real_encode_embeds_tags_and_square_cover(tmp_path):
    audio, thumb = tmp_path / "audio.opus", tmp_path / "thumb.webp"
    gen = ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi"]
    subprocess.run(
        gen + ["-i", "sine=frequency=440:duration=5", "-c:a", "libopus", str(audio)], check=True
    )
    subprocess.run(gen + ["-i", "testsrc=size=1280x720", "-frames:v", "1", str(thumb)], check=True)

    cover = media.square_cover(thumb, tmp_path / "cover.jpg")
    seen = []
    tags = media.Tags(title="Sunset · Mix", artist="DJ K", genre="House", comment="hi")
    out = media.encode_mp3(audio, cover, tags, tmp_path / "out.mp3", 5.0, seen.append)

    probe = json.loads(
        subprocess.run(
            ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(out)],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )
    t = probe["format"]["tags"]
    assert (t["title"], t["artist"], t["genre"]) == ("Sunset · Mix", "DJ K", "House")
    streams = {s["codec_type"]: s for s in probe["streams"]}
    assert streams["audio"]["codec_name"] == "mp3"
    pic = streams["video"]
    assert pic["disposition"]["attached_pic"] == 1
    assert pic["width"] == pic["height"] == 720
    assert seen and seen[-1] > 0.9
    assert not list(tmp_path.glob("*.untagged.mp3"))


@needs_ffmpeg
def test_ffmpeg_error_is_reported(tmp_path):
    bad = tmp_path / "not-audio.webm"
    bad.write_text("nope")
    with pytest.raises(media.EncodeFailed, match="ffmpeg"):
        media.encode_mp3(bad, None, media.Tags("t", "a", "g"), tmp_path / "o.mp3", 1.0)
