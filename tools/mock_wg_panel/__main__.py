"""Run the mock panel: ``python -m mock_wg_panel --host 127.0.0.1 --port 8787``.

Equivalent uvicorn invocation::

    uvicorn "mock_wg_panel.app:create_app" --factory --host 127.0.0.1 --port 8787
"""

from __future__ import annotations

import argparse
import logging

from .app import create_app
from .store import MockConfig

LOGGER = logging.getLogger("mock_wg_panel")


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and serve the mock panel with uvicorn."""
    parser = argparse.ArgumentParser(prog="mock_wg_panel", description="In-memory WG-Guard panel mock")
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8787, help="bind port (default: 8787)")
    parser.add_argument("--log-level", default="info", help="uvicorn log level (default: info)")
    parser.add_argument(
        "--clock-offset",
        type=float,
        default=0.0,
        help="seconds added to the mock clock; negative values travel to the past",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=str(args.log_level).upper(), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    import uvicorn  # imported lazily so --help works without uvicorn installed

    config = MockConfig.default()
    if args.clock_offset:
        config = config.with_clock_offset(args.clock_offset)
    LOGGER.info("serving mock WG-Guard panel on http://%s:%d", args.host, args.port)
    uvicorn.run(create_app(config), host=args.host, port=args.port, log_level=args.log_level)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
