"""Stable identifiers shared by reducer, job, and presentation boundaries."""

from __future__ import annotations

from enum import StrEnum


class ProgressStage(StrEnum):
    PREPARING_MODEL = "preparing_model"
    DOWNLOADING_MODEL = "downloading_model"
    SEGMENTATION = "segmentation"
    DECODE = "decode"
    RENDER_CUT = "render_cut"
    FRAMING = "framing"
    CUT_PROMOTION = "cut_promotion"
    VALIDATION = "validation"
    POST_PROCESS = "post_process"
    AUTO_FIT = "auto_fit"
    ENCODE = "encode"
    VALIDATE = "validate"
    COMPLETE = "complete"
    CANCELLING = "cancelling"


class PreviewInvalidationReason(StrEnum):
    SEGMENTATION = "segmentation"
    COMPUTE_ACCELERATION = "compute_acceleration"
    SAMPLING = "sampling"
    CROP_CLEANUP = "crop_cleanup"
    CROP = "crop"
    FRAMING = "framing"
    PLAYHEAD = "playhead"
    EXPORT_RANGE = "export_range"
    PREVIEW_FAILED = "preview_failed"
    EDITED_CUTS = "edited_cuts"
