"""``af`` command-line entry point.

Phase 0 wires the nine subcommands (R18). U1 ships the argument surface with stub
handlers; later units (U2-U9) replace each stub with real behavior. Every command
exits non-zero with an actionable message on failure.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

SUBCOMMANDS: dict[str, str] = {
    "init": "Seed families/agents, write thresholds.toml, migrate schema, prefetch embedder.",
    "add-idea": "Register a hand-written idea through the routing pipeline.",
    "promote": "Promote a batch's quarantined insights to active.",
    "revert": "Revert a batch's insights to retired.",
    "retire": "Retire an insight or skill by id.",
    "revive": "Revive a retired insight or skill by id.",
    "render": "Render a skill (concatenation, or --compile for delta-patch).",
    "export": "Export skills as Claude Code SKILL.md files.",
    "status": "Report library counts, batches, flags, snapshot, and config.",
}


class _NotYetImplemented(SystemExit):
    """Raised by a stub handler until the owning unit lands it."""

    def __init__(self, command: str) -> None:
        super().__init__(
            f"af {command}: not yet implemented in this Phase 0 build "
            f"(see the unit owning '{command}' in PROGRESS.md)."
        )


def _stub(command: str):
    def handler(_args: argparse.Namespace) -> int:
        raise _NotYetImplemented(command)

    return handler


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="af",
        description="agent-families — self-improving skill library (Phase 0 core).",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")
    subparsers.required = True
    for name, help_text in SUBCOMMANDS.items():
        sub = subparsers.add_parser(name, help=help_text, description=help_text)
        sub.set_defaults(_handler=_stub(name))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "_handler", None)
    if handler is None:  # pragma: no cover - argparse enforces a subcommand
        parser.print_help()
        return 2
    return handler(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
