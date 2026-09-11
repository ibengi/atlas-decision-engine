#!/usr/bin/env python3
"""Alias entrypoint: PRODUCTION READ_ONLY, loop. Patches nothing.

This file exists so that a platform start command pointing at it keeps
working. It is byte-for-byte equivalent to the repository's own entrypoints::

    Dockerfile CMD  python kalshi_alpha_bot.py --loop --live-read-only
    Procfile        python kalshi_alpha_bot.py --loop --live-read-only

Every READ_ONLY semantic -- observation through `equity_drawdown`, the
`would_block_capital` evidence, the dashboard snapshot at startup -- lives in
the application itself (`execution_engine.ExecutionEngine`,
`kalshi_alpha_bot.main`) and is selected by ``--live-read-only`` alone. A
launch through this file, through the Dockerfile, through the Procfile or by
hand runs the same code with the same arguments; nothing here can add,
remove or alter a gate. `tests/test_entrypoint_equivalence.py` pins that.
"""

import sys

import kalshi_alpha_bot as bot

#: The canonical READ_ONLY arguments, shared with Dockerfile and Procfile.
CANONICAL_ARGV = ("--loop", "--live-read-only")


def main():
    sys.argv = [sys.argv[0], *CANONICAL_ARGV]
    bot.main()


if __name__ == "__main__":
    main()
