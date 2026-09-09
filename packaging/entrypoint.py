"""Native bundle entry point kept outside the importable application package."""

from __future__ import annotations

import re
import sys

_RESOURCE_TRACKER_PAYLOAD = re.compile(
    r"from multiprocessing\.resource_tracker import main;main\(([0-9]+)\)\Z"
)


def _prepare_multiprocessing_payload(argv: list[str]) -> int | None:
    """Return a safe multiprocessing payload and remove its interpreter args."""
    try:
        code_index = argv.index("-c")
    except ValueError:
        return None

    if code_index + 1 >= len(argv):
        raise ValueError("matteloop: interpreter -c argument is missing its code")

    payload = argv[code_index + 1]
    match = _RESOURCE_TRACKER_PAYLOAD.fullmatch(payload)
    if match is None:
        raise ValueError(
            "matteloop: refusing to execute unsupported interpreter payload; "
            "only the multiprocessing resource-tracker bootstrap is supported"
        )

    del argv[1 : code_index + 2]
    return int(match.group(1))


if __name__ == "__main__":
    try:
        interpreter_fd = _prepare_multiprocessing_payload(sys.argv)
    except ValueError as error:
        raise SystemExit(str(error)) from None
    if interpreter_fd is not None:
        from multiprocessing.resource_tracker import main as resource_tracker_main

        resource_tracker_main(interpreter_fd)
    else:
        from velopack import App

        App().set_auto_apply_on_startup(False).run()
        from matteloop.app import main

        raise SystemExit(main())
