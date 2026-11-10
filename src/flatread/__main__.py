"""``python -m flatread scaffold PATH --format FORMAT``: print a row struct and a layout read off a sample body."""

import argparse
import pathlib
import sys

from flatread.scaffold import scaffold


def main(argv: list[str] | None = None) -> int:
    """Run the command line; return its exit status."""
    parser = argparse.ArgumentParser(prog="python -m flatread")
    commands = parser.add_subparsers(dest="command", required=True)
    scaffolding = commands.add_parser("scaffold", help="print a row struct and a layout for a sample body")
    scaffolding.add_argument("path", type=pathlib.Path)
    scaffolding.add_argument("--format", required=True, choices=["csv", "pipe", "tsv", "sheet", "json"])
    scaffolding.add_argument("--sheet")
    scaffolding.add_argument("--records")
    scaffolding.add_argument("--name", help="the layout's class name (default: the file's name)")
    args = parser.parse_args(argv)
    name = args.name or "".join(part.title() for part in args.path.stem.replace("-", "_").split("_") if part)
    sys.stdout.write(
        scaffold(args.path.read_bytes(), format=args.format, name=name, sheet=args.sheet, records=args.records)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
