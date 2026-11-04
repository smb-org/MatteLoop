"""Playback decoder tests: decode seams, conversion fidelity, thread safety."""

from __future__ import annotations

import threading
from fractions import Fraction
from pathlib import Path

import av
import pytest
from PIL import Image, ImageStat

from matteloop.jobs.playback import PlaybackDecoder
from matteloop.jobs.source import decode_frame, probe_source
from tests.fixtures.media_factory import make_video

# Mean absolute per-channel difference (0-255 scale) allowed between
# ``PlaybackDecoder.convert`` and the full-resolution still resized by Pillow
# LANCZOS. LANCZOS stands in for the canvas's Qt smooth scaling, which a Qt-free
# test cannot call. Measured (mean, worst quadrant mean):
#   bt709 transfer 96x64 -> 48x32:      mean 3.109, quadrant 2.914
#   oriented 8x32 -> 4x16:               mean 2.833, quadrant 4.250
#   sar2 32x16 -> 32x8:                  mean 1.417, quadrant 2.125
#   unaligned yuv 40x24 -> 15x9:         mean 0.111, quadrant 0.200
# TOLERANCE is the largest mean rounded up (4) plus 2.
TOLERANCE = 6.0


def _declare_srgb(stream, *, rgb: bool) -> None:
    stream.codec_context.color_primaries = 1
    stream.codec_context.color_trc = 13
    stream.codec_context.colorspace = 0 if rgb else 1
    stream.codec_context.color_range = 2 if rgb else 1


def _make_seekable_video(path: Path) -> Path:
    """120 frames at 20 fps, GOP 10, red channel ramps with the frame index."""
    with av.open(str(path), "w") as container:
        stream = container.add_stream("libx264rgb", rate=20)
        stream.width = 16
        stream.height = 8
        stream.pix_fmt = "rgb24"
        stream.options = {"g": "10", "keyint_min": "10", "sc_threshold": "0"}
        _declare_srgb(stream, rgb=True)
        for index in range(120):
            frame = av.VideoFrame.from_image(Image.new("RGB", (16, 8), (index, 0, 0)))
            frame.pts = index
            frame.time_base = Fraction(1, 20)
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    return path


_COLOURS = ("red", "lime", "blue", "yellow", "cyan", "magenta")


def _make_yuv_video(path: Path, size: tuple[int, int]) -> Path:
    frames = [Image.new("RGB", size, colour) for colour in _COLOURS]
    return make_video(path, frames, Fraction(20))


def _gradient(size: tuple[int, int], shift: int) -> Image.Image:
    image = Image.new("RGB", size)
    width, height = size
    for x in range(width):
        value = (x * 255 // max(1, width - 1) + shift * 20) % 256
        for y in range(height):
            image.putpixel(
                (x, y), (value, 255 - value, (y * 255) // max(1, height - 1))
            )
    return image


def _make_bt709_transfer_video(path: Path) -> Path:
    """96x64 horizontal gradient tagged BT.709 transfer (color_trc 1)."""
    with av.open(str(path), "w") as container:
        stream = container.add_stream("libx264", rate=20)
        stream.width = 96
        stream.height = 64
        stream.pix_fmt = "yuv420p"
        stream.codec_context.time_base = Fraction(1, 20)
        stream.codec_context.color_primaries = 1
        stream.codec_context.color_trc = 1
        stream.codec_context.colorspace = 1
        stream.codec_context.color_range = 1
        for index in range(6):
            frame = av.VideoFrame.from_image(_gradient((96, 64), index))
            frame.pts = index
            frame.time_base = Fraction(1, 20)
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    return path


def _make_oriented_video(
    path: Path, *, rotate: bool = True, size: tuple[int, int] = (16, 8)
) -> Path:
    """Lossless RGB clip with SAR 2 and optionally ``rotate=90`` metadata."""
    width, height = size
    frames = [
        Image.new("RGB", size, "red"),
        Image.new("RGB", size, "lime"),
        Image.new("RGB", size, "blue"),
    ]
    for y in range(height):
        for x in range(width // 2, width):
            frames[0].putpixel((x, y), (0, 0, 255))
    with av.open(str(path), "w") as container:
        stream = container.add_stream("libx264rgb", rate=20)
        stream.width = width
        stream.height = height
        stream.pix_fmt = "rgb24"
        stream.codec_context.time_base = Fraction(1, 20)
        _declare_srgb(stream, rgb=True)
        stream.sample_aspect_ratio = Fraction(2)
        stream.codec_context.sample_aspect_ratio = Fraction(2)
        if rotate:
            stream.metadata["rotate"] = "90"
        for index, image in enumerate(frames):
            frame = av.VideoFrame.from_image(image)
            frame.pts = index
            frame.time_base = Fraction(1, 20)
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    return path


def _decoder(path: Path, display_size: tuple[int, int] | None = None):
    info = probe_source(path)
    decoder = PlaybackDecoder(
        path,
        info.validation_proof,
        info.revision,
        display_size or (info.width, info.height),
    )
    return decoder, info


def _still(path: Path, info, timestamp: Fraction):
    return decode_frame(path, timestamp, 1, validation_proof=info.validation_proof)


def _frame_at(decoder: PlaybackDecoder, timestamp: Fraction):
    decoder.seek(timestamp)
    return next(iter(decoder.frames()))


def _quadrant_means(image: Image.Image) -> list[list[float]]:
    width, height = image.size
    boxes = [
        (0, 0, width // 2, height // 2),
        (width // 2, 0, width, height // 2),
        (0, height // 2, width // 2, height),
        (width // 2, height // 2, width, height),
    ]
    return [ImageStat.Stat(image.convert("RGB").crop(box)).mean for box in boxes]


def _mean_abs_difference(a: Image.Image, b: Image.Image) -> float:
    assert a.size == b.size
    left, right = a.convert("RGB").tobytes(), b.convert("RGB").tobytes()
    return sum(abs(x - y) for x, y in zip(left, right)) / len(left)


def _worst_quadrant_difference(a: Image.Image, b: Image.Image) -> float:
    return max(
        abs(x - y)
        for qa, qb in zip(_quadrant_means(a), _quadrant_means(b))
        for x, y in zip(qa, qb)
    )


def test_frames_from_a_seek_start_with_the_owning_frame_and_continue_in_order(
    tmp_path,
):
    path = _make_seekable_video(tmp_path / "seekable.mp4")
    decoder, info = _decoder(path)
    target = Fraction(7, 20) + Fraction(1, 40)
    expected = _still(path, info, target).actual_pts
    decoder.open()
    try:
        decoder.seek(target)
        pts = [value for value, _ in decoder.frames()]
    finally:
        decoder.close()
    assert pts[0] == expected == Fraction(7, 20)
    assert len(pts) == 120 - 7
    assert all(
        later - earlier == Fraction(1, 20) for earlier, later in zip(pts, pts[1:])
    )
    assert pts[-1] == Fraction(119, 20)


@pytest.mark.parametrize(
    "maker",
    [
        lambda p: _make_yuv_video(p, (64, 32)),
        lambda p: _make_yuv_video(p, (40, 24)),
        _make_bt709_transfer_video,
    ],
    ids=["aligned", "unaligned", "bt709-transfer"],
)
def test_convert_matches_decode_frame_exactly_at_source_size(tmp_path, maker):
    path = maker(tmp_path / "clip.mp4")
    decoder, info = _decoder(path)
    decoder.open()
    try:
        for timestamp in (Fraction(0), Fraction(3, 20)):
            _, frame = _frame_at(decoder, timestamp)
            converted = decoder.convert(frame)
            still = _still(path, info, timestamp).image
            assert (converted.width, converted.height) == still.size
            assert converted.rgba == still.tobytes()
            assert len(converted.rgba) == converted.width * converted.height * 4
    finally:
        decoder.close()


def test_convert_keeps_the_stills_orientation_and_aspect(tmp_path):
    path = _make_oriented_video(tmp_path / "oriented.mkv")
    decoder, info = _decoder(path, (8, 32))
    assert (info.width, info.height, info.rotation, info.pixel_aspect) == (
        8,
        32,
        270,
        Fraction(2),
    )
    assert decoder.target_size == (8, 32)
    decoder.open()
    try:
        _, frame = _frame_at(decoder, Fraction(0))
        converted = decoder.convert(frame)
    finally:
        decoder.close()
    still = _still(path, info, Fraction(0)).image
    assert (converted.width, converted.height) == still.size
    image = Image.frombytes("RGBA", (converted.width, converted.height), converted.rgba)
    for ours, theirs in zip(_quadrant_means(image), _quadrant_means(still)):
        assert all(abs(a - b) <= 2 for a, b in zip(ours, theirs))


def _downscale_cases(tmp_path: Path):
    return {
        "bt709 transfer 96x64 -> 48x32": (
            _make_bt709_transfer_video(tmp_path / "a.mp4"),
            (48, 32),
        ),
        "oriented 8x32 -> 4x16": (_make_oriented_video(tmp_path / "b.mkv"), (4, 16)),
        "sar2 32x16 -> 32x8": (
            _make_oriented_video(tmp_path / "c.mkv", rotate=False, size=(32, 16)),
            (32, 8),
        ),
        "unaligned yuv 40x24 -> 15x9": (
            _make_yuv_video(tmp_path / "d.mp4", (40, 24)),
            (15, 9),
        ),
    }


def test_downscaled_convert_approximates_the_resized_still(tmp_path):
    for name, (path, display) in _downscale_cases(tmp_path).items():
        decoder, info = _decoder(path, display)
        decoder.open()
        try:
            _, frame = _frame_at(decoder, Fraction(0))
            converted = decoder.convert(frame)
        finally:
            decoder.close()
        size = decoder.target_size
        assert (converted.width, converted.height) == size, name
        ours = Image.frombytes("RGBA", size, converted.rgba)
        still = _still(path, info, Fraction(0)).image
        reference = still.resize(size, Image.Resampling.LANCZOS)
        mean = _mean_abs_difference(ours, reference)
        worst = _worst_quadrant_difference(ours, reference)
        print(f"MEASURED {name}: mean={mean:.3f} worst_quadrant={worst:.3f}")
        assert mean <= TOLERANCE, name


def test_seek_inside_an_open_container_reapplies_the_preroll(tmp_path):
    path = _make_seekable_video(tmp_path / "seekable.mp4")
    decoder, info = _decoder(path)
    expected = _still(path, info, Fraction(3, 10)).actual_pts
    decoder.open()
    try:
        decoder.seek(Fraction(5, 2))
        iterator = decoder.frames()
        for _ in range(3):
            next(iterator)
        decoder.seek(Fraction(3, 10))
        first, _ = next(iter(decoder.frames()))
    finally:
        decoder.close()
    assert first == expected == Fraction(3, 10)


def _open_decode_close(path: Path) -> None:
    decoder, _ = _decoder(path)
    decoder.open()
    try:
        decoder.seek(Fraction(0))
        iterator = decoder.frames()
        for _ in range(3):
            _, frame = next(iterator)
            decoder.convert(frame)
    finally:
        decoder.close()


def _run_thread(target, errors: list[BaseException]) -> threading.Thread:
    def runner() -> None:
        try:
            target()
        except BaseException as error:  # noqa: BLE001 - re-raised by the test
            errors.append(error)

    thread = threading.Thread(target=runner)
    thread.start()
    return thread


def test_repeated_open_decode_close_on_worker_threads_is_safe(tmp_path):
    path = _make_yuv_video(tmp_path / "aligned.mp4", (64, 32))
    info = probe_source(path)
    errors: list[BaseException] = []
    for _ in range(20):
        _run_thread(lambda: _open_decode_close(path), errors).join()
    competing = _run_thread(
        lambda: decode_frame(
            path, Fraction(0), 1, validation_proof=info.validation_proof
        ),
        errors,
    )
    _run_thread(lambda: _open_decode_close(path), errors).join()
    competing.join()
    if errors:
        raise errors[0]


def test_close_is_idempotent_and_frames_before_seek_is_an_error(tmp_path):
    path = _make_yuv_video(tmp_path / "aligned.mp4", (64, 32))
    decoder, _ = _decoder(path)
    decoder.open()
    with pytest.raises(RuntimeError):
        decoder.frames()
    decoder.close()
    decoder.close()
