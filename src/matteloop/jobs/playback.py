"""Qt-free sequential decoder that feeds source playback.

The private helpers imported from ``matteloop.jobs.source`` are the still-decode
seams: container ownership, stream resolution from the validation proof, the
backward seek, the per-frame colour contract and the width padding libswscale
needs. Re-exporting them would grow a frozen module, so this module imports
them by name instead.

Playback is an intentionally approximate viewing transform. ``convert`` scales
during the swscale conversion and applies the transfer LUT and rotation to the
small image, where the still path converts at full resolution first. Scaling
uses PyAV's default interpolation, which ``av.video.reformatter`` documents as
``BILINEAR`` (``SWS_BILINEAR``); no interpolation argument is passed.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import ExitStack
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

from av.video.reformatter import ColorRange, Colorspace, VideoReformatter
from PIL import Image

from matteloop.core.errors import AppError, ErrorCode
from matteloop.jobs.source import (
    _SWSCALE_WIDTH_ALIGNMENT,
    SourceRevision,
    SourceValidationProof,
    _convert_bt709_transfer_to_srgb,
    _frame_timestamp,
    _input_container,
    _profile_contract,
    _proof_color_contract,
    _seek_for_timestamp,
    _stream_from_proof,
    _validate_frame_color,
    _width_padded_yuv_frame,
)

_COLORSPACES = {
    1: Colorspace.ITU709,
    5: Colorspace.ITU601,
    6: Colorspace.ITU601,
}
_TRANSPOSES = {
    90: Image.Transpose.ROTATE_90,
    180: Image.Transpose.ROTATE_180,
    270: Image.Transpose.ROTATE_270,
}


@dataclass(frozen=True, slots=True)
class PlaybackFrame:
    """One display-sized frame as tightly packed RGBA8888 (``width * 4`` per row)."""

    pts: Fraction
    rgba: bytes
    width: int
    height: int


def _round_half_up(numerator: int, denominator: int) -> int:
    return (2 * numerator + denominator) // (2 * denominator)


def _fit_size(oriented: tuple[int, int], display: tuple[int, int]) -> tuple[int, int]:
    """Largest size of ``oriented``'s aspect inside ``display``; never upscales."""
    oriented_width, oriented_height = oriented
    display_width, display_height = display
    if display_width >= oriented_width and display_height >= oriented_height:
        return oriented
    if display_width * oriented_height <= display_height * oriented_width:
        width = display_width
        height = _round_half_up(display_width * oriented_height, oriented_width)
    else:
        height = display_height
        width = _round_half_up(display_height * oriented_width, oriented_height)
    return max(1, width), max(1, height)


def _geometry(
    proof: SourceValidationProof, display_size: tuple[int, int]
) -> tuple[tuple[int, int], tuple[int, int]]:
    """Return ``(target_size, pre_transpose_size)`` for a proof and a display."""
    if display_size[0] < 1 or display_size[1] < 1:
        raise ValueError("display_size must be at least 1x1")
    pixel_aspect = proof.pixel_aspect
    width = max(
        1,
        _round_half_up(
            proof.coded_width * pixel_aspect.numerator, pixel_aspect.denominator
        ),
    )
    swapped = proof.rotation in {90, 270}
    oriented = (proof.coded_height, width) if swapped else (width, proof.coded_height)
    target = _fit_size(oriented, display_size)
    return target, ((target[1], target[0]) if swapped else target)


def _color_mismatch(detail: str) -> AppError:
    return AppError(
        ErrorCode.SOURCE_CORRUPT,
        "source",
        "source.decode.failed",
        detail,
        "reload-source",
    )


class PlaybackDecoder:
    """Decode one source sequentially from a seek and convert at display size.

    The decoder is not thread-safe: open, decode and close it on one thread.
    """

    def __init__(
        self,
        path: Path,
        proof: SourceValidationProof,
        revision: SourceRevision,
        display_size: tuple[int, int],
    ) -> None:
        self._path = path
        self._proof = proof
        self._revision = revision
        self._target_size, self._pre_size = _geometry(proof, display_size)
        self._stack: ExitStack | None = None
        self._container: Any = None
        self._stream: Any = None
        self._pending_target: Fraction | None = None

    @property
    def target_size(self) -> tuple[int, int]:
        """Oriented output size of :meth:`convert`, fixed at construction."""
        return self._target_size

    def open(self) -> None:
        """Open the container and resolve the proof's stream."""
        if self._stack is not None:
            raise RuntimeError("decoder is already open")
        stack = ExitStack()
        try:
            container, revision = stack.enter_context(
                _input_container(self._path, self._revision)
            )
            self._stream = _stream_from_proof(container, self._proof, revision)
        except BaseException:
            stack.close()
            raise
        self._container = container
        self._stack = stack

    def seek(self, timestamp: Fraction) -> None:
        """Seek to the keyframe before ``timestamp`` and arm the pre-roll."""
        if self._stack is None:
            raise RuntimeError("seek requires open()")
        _seek_for_timestamp(
            self._container,
            self._stream,
            timestamp,
            self._proof.presentation_origin,
            self._proof.duration,
        )
        self._pending_target = timestamp

    def frames(self) -> Iterator[tuple[Fraction, Any]]:
        """Yield ``(public_pts, frame)`` from the frame owning the seek target."""
        if self._stack is None or self._pending_target is None:
            raise RuntimeError("frames requires open() and seek()")
        return self._frames_from(self._pending_target)

    def _frames_from(self, target: Fraction) -> Iterator[tuple[Fraction, Any]]:
        candidate: tuple[Fraction, Any] | None = None
        released = False
        for frame in self._container.decode(self._stream):
            public_pts = self._public_pts(frame)
            if public_pts is None:
                continue
            if released:
                yield public_pts, frame
            elif public_pts <= target:
                candidate = (public_pts, frame)
            else:
                released = True
                yield candidate or (public_pts, frame)
                if candidate is not None:
                    yield public_pts, frame
        if not released and candidate is not None:
            yield candidate

    def _public_pts(self, frame: Any) -> Fraction | None:
        profile = _validate_frame_color(self._stream, frame)
        if _profile_contract(profile) != _proof_color_contract(self._proof):
            raise _color_mismatch("decoded frame color differs from validation proof")
        if frame.pts is None or frame.time_base is None:
            return None
        public_pts = _frame_timestamp(frame) - self._proof.presentation_origin
        return public_pts if public_pts >= 0 else None

    def convert(self, frame: Any) -> PlaybackFrame:
        """Convert a decoded frame: scale, colour, transfer LUT, rotation."""
        image = self._reformat(frame)
        try:
            if self._proof.color_transfer in {1, 6}:
                transferred = _convert_bt709_transfer_to_srgb(image)
                image.close()
                image = transferred
            transpose = _TRANSPOSES.get(self._proof.rotation)
            if transpose is not None:
                transposed = image.transpose(transpose)
                image.close()
                image = transposed
            return PlaybackFrame(
                _frame_timestamp(frame) - self._proof.presentation_origin,
                image.tobytes(),
                image.width,
                image.height,
            )
        finally:
            image.close()

    def _reformat(self, frame: Any) -> Image.Image:
        """Scale to the pre-transpose size and return it as an RGBA image."""
        width, height = self._pre_size
        if self._proof.rgb_input:
            converted: Any = frame.reformat(width=width, height=height, format="rgba")
            image: Image.Image = converted.to_image()
            try:
                return image.convert("RGBA")
            finally:
                image.close()
        padded, _ = _width_padded_yuv_frame(frame)
        scaled_width, visible_width = self._yuv_widths(padded.width, frame.width)
        converted_yuv: Any = VideoReformatter().reformat(
            padded,
            width=scaled_width,
            height=height,
            format="rgba",
            src_colorspace=_COLORSPACES[self._proof.color_matrix],
            dst_colorspace=Colorspace.ITU709,
            src_color_range=(
                ColorRange.JPEG if self._proof.color_range == 2 else ColorRange.MPEG
            ),
            dst_color_range=ColorRange.JPEG,
        )
        image = converted_yuv.to_image()
        try:
            if visible_width != width:
                # Resample only the visible columns to the exact target width.
                resized = image.resize(
                    (width, height),
                    Image.Resampling.BILINEAR,
                    box=(0, 0, visible_width, height),
                )
                image.close()
                image = resized
            elif scaled_width != width:
                cropped = image.crop((0, 0, width, height))
                image.close()
                image = cropped
            return image.convert("RGBA")
        finally:
            image.close()

    def _yuv_widths(self, padded_width: int, source_width: int) -> tuple[int, int]:
        """Return the aligned destination width and the visible part of it.

        libswscale writes past the end of an unaligned RGBA destination (see
        ``_width_padded_yuv_frame``), so the destination width is rounded up to
        ``_SWSCALE_WIDTH_ALIGNMENT``. The padded source scales as a whole, so
        the picture occupies only the first ``visible`` columns of it.
        """
        width = self._pre_size[0]
        wanted = -(-padded_width * width // source_width)
        blocks = -(-wanted // _SWSCALE_WIDTH_ALIGNMENT)
        scaled = max(width, blocks * _SWSCALE_WIDTH_ALIGNMENT)
        visible = _round_half_up(source_width * scaled, padded_width)
        return scaled, min(scaled, max(width, visible))

    def close(self) -> None:
        """Close the container; an ``AppError`` from its exit propagates."""
        stack, self._stack = self._stack, None
        self._container = None
        self._stream = None
        self._pending_target = None
        if stack is not None:
            stack.close()
