"""Standalone engine entry point."""
import argparse
import sys
from .cli import register_rpg_maker_mv_parser

def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m game_l10n.rpg_maker_mv")
    commands = parser.add_subparsers(dest="sdk_command", required=True)
    register_rpg_maker_mv_parser(commands)
    args = parser.parse_args(["rpg-maker-mv", *(sys.argv[1:] if argv is None else argv)])
    return args.func(args)

if __name__ == "__main__":
    raise SystemExit(main())
