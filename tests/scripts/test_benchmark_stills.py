from __future__ import annotations

from pathlib import Path

from PIL import Image

from scripts.benchmark_stills import extract_still_frame, still_relative_path


def _write_animated_webp(path: Path) -> None:
    frame0 = Image.new("RGBA", (4, 4), (255, 0, 0, 255))
    frame1 = Image.new("RGBA", (4, 4), (0, 255, 0, 255))
    frame0.save(
        path,
        format="WEBP",
        save_all=True,
        append_images=[frame1],
        duration=100,
        loop=0,
    )


def test_extract_still_frame_writes_a_png_for_an_animated_webp(tmp_path: Path) -> None:
    webp_path = tmp_path / "model.webp"
    still_path = tmp_path / "model-still.png"
    _write_animated_webp(webp_path)

    assert extract_still_frame(webp_path, still_path) is True

    assert still_path.exists()
    with Image.open(still_path) as still:
        assert still.format == "PNG"
        assert still.size == (4, 4)
        assert still.mode == "RGBA"


def test_extract_still_frame_degrades_on_a_missing_webp(tmp_path: Path) -> None:
    webp_path = tmp_path / "does-not-exist.webp"
    still_path = tmp_path / "does-not-exist-still.png"

    assert extract_still_frame(webp_path, still_path) is False

    assert not still_path.exists()


def test_extract_still_frame_degrades_on_a_corrupt_webp(tmp_path: Path) -> None:
    webp_path = tmp_path / "corrupt.webp"
    webp_path.write_bytes(b"not actually a webp file")
    still_path = tmp_path / "corrupt-still.png"

    assert extract_still_frame(webp_path, still_path) is False

    assert not still_path.exists()


def test_still_relative_path_is_named_after_the_model() -> None:
    assert still_relative_path("birefnet-portrait") == "birefnet-portrait-still.png"
