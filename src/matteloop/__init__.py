"""MatteLoop package."""

import os

from PIL import Image

# The runtime starts Microsoft's 1DS telemetry SDK on import. Without this,
# it records a hardware-fingerprint event per launch and uploads it; a
# packaged 0.4.0 build produced a crash report at quit inside that SDK's
# teardown. This variable must be set before the first onnxruntime import in
# every process, which is why it lives here rather than in load_onnxruntime().
os.environ.setdefault("ORT_DISABLE_TELEMETRY", "1")

__version__ = "0.4.0"


def application_title() -> str:
    """Return the user-visible application title for this build."""
    return f"MatteLoop {__version__}"

# Pillow warns above this value and errors above twice this value. Align the
# warning boundary with the largest legal MatteLoop canvas without disabling the
# decompression-bomb protection globally.
PILLOW_MAX_IMAGE_PIXELS = 16_383**2
Image.MAX_IMAGE_PIXELS = PILLOW_MAX_IMAGE_PIXELS
