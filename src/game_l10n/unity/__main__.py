"""Standalone engine entry point."""
import argparse
import sys
from .cli import register_unity_parser

def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m game_l10n.unity")
    commands = parser.add_subparsers(dest="sdk_command", required=True)
    register_unity_parser(commands)
    args = parser.parse_args(["unity", *(sys.argv[1:] if argv is None else argv)])
    return args.func(args)

if __name__ == "__main__":
    raise SystemExit(main())
