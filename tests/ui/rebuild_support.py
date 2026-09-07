"""The stand-in manifest both rebuild-route test modules drive the picker with."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RebuildManifest:
    """Duck-typed stand-in exposing only what request_for_workspace reads."""

    cache_key_inputs: dict[str, object]
    source_path: str


def rebuild_manifest(source: Path) -> RebuildManifest:
    return RebuildManifest(
        cache_key_inputs={
            "sampling": {
                "start": {"numerator": 0, "denominator": 1},
                "end": {"numerator": 2, "denominator": 1},
                "fps": 15,
            },
            "crop": {"x": 0, "y": 0, "width": 128, "height": 128},
            "model": {"id": "u2net"},
            "edge_settings": {
                "mode": "standard",
                "alpha_matting": {
                    "foreground_threshold": 240,
                    "background_threshold": 10,
                    "erode_size": 10,
                },
            },
        },
        source_path=str(source),
    )
