import argparse
import logging
import os
import sys
from pathlib import Path

from .config import load_config
from .server import create_server


def main() -> None:
    parser = argparse.ArgumentParser(description="Windows webcam recorder MCP (stdio)")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--check-config", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    if os.name != "nt":
        parser.error("This version requires Windows DirectShow")
    try:
        config = load_config(args.config)
    except (OSError, ValueError) as exc:
        print(f"Invalid recorder configuration: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    if args.check_config:
        print("Recorder configuration valid", file=sys.stderr)
        return
    create_server(config).run(transport="stdio")


if __name__ == "__main__":
    main()
