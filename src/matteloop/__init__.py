"""MatteLoop package."""

import os

from PIL import Image

# The runtime starts Microsoft's 1DS telemetry SDK on import. Without this,
# it records a hardware-fingerprint event per launch and uploads it; a
# packaged 0.4.0 build produced a crash report at quit inside that SDK's
# teardown. This variable is documented in ONNX Runtime's docs/Privacy.md
# under "Disabling Telemetry" and read in PosixTelemetry::Initialize(); it
# must be set before the first onnxruntime import in every process, which is
# why it lives here rather than in load_onnxruntime(). Assigned unconditionally:
# MatteLoop offers no telemetry opt-in, so an inherited ORT_DISABLE_TELEMETRY=0
# or empty value is never intentional and must not survive the import.
os.environ["ORT_DISABLE_TELEMETRY"] = "1"

__version__ = "0.4.0"

# The single source for the product name. QSettings identity and the window
# title derive from it in code; the packaging spec's --macos-app-name and
# --product-name are checked against it by test_version_identity.py.
APPLICATION_NAME = "MatteLoop"


def application_title() -> str:
    """Return the user-visible application title for this build."""
    return f"{APPLICATION_NAME} {__version__}"

# Pillow warns above this value and errors above twice this value. Align the
# warning boundary with the largest legal MatteLoop canvas without disabling the
# decompression-bomb protection globally.
PILLOW_MAX_IMAGE_PIXELS = 16_383**2
Image.MAX_IMAGE_PIXELS = PILLOW_MAX_IMAGE_PIXELS
