#!/usr/bin/env python3
"""Entry point for the Atlas Alpha Shadow Service. SHADOW ONLY.

    python tools/alpha_service_run.py run            # continuous
    python tools/alpha_service_run.py once           # one poll
    python tools/alpha_service_run.py health         # providers + budget
    python tools/alpha_service_run.py telemetry
    python tools/alpha_service_run.py resolve --prediction-id ... --outcome 1

DEPLOY THIS AS ITS OWN SERVICE (section 2)
    It must receive XAI_API_KEY, GEMINI_API_KEY and OPENAI_API_KEY,
    and MUST NOT receive any broker credential. The process refuses to start
    if one is visible in its environment -- the separation is the point, so
    it is checked rather than assumed.

    It shares one directory with the engine (`DATA_DIR`): the engine writes
    research candidates into `research_spool/`, this service reads them and
    writes its own ledgers. Nothing else crosses.

    This process holds no broker client and has no code path to one.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from alpha_service import (AlphaShadowService,               # noqa: E402
                           BrokerCredentialsPresent,
                           assert_no_broker_credentials)
from alpha_telemetry import Telemetry                        # noqa: E402
from config import CFG                                       # noqa: E402
from logging_config import setup_logging                     # noqa: E402


#: Grep-able proof that the startup guard fired. A deployment that
#: crash-loops on this line is a deployment that is correctly refusing
#: to run Alpha beside broker authority.
STARTUP_REFUSED_MARKER = "ALPHA_STARTUP_REFUSED_BROKER_CREDENTIALS"


def _service() -> AlphaShadowService:
    return AlphaShadowService()


def cmd_health(args) -> int:
    service = _service()
    print(json.dumps(service.startup_report(), indent=2, default=str))
    return 0


def cmd_once(args) -> int:
    service = _service()
    service.startup_report()
    print(json.dumps(service.cycle(limit=args.limit), indent=2, default=str))
    return 0


def cmd_run(args) -> int:
    service = _service()
    service.install_signal_handlers()
    report = service.run(max_cycles=args.max_cycles)
    print(json.dumps(report, indent=2, default=str))
    return 0


def cmd_telemetry(args) -> int:
    path = os.path.join(CFG.DATA_DIR, CFG.ALPHA_TELEMETRY_FILE)
    if not os.path.exists(path):
        print(json.dumps(Telemetry().snapshot(), indent=2, default=str))
        return 0
    with open(path, encoding="utf-8") as fh:
        print(fh.read())
    return 0


def cmd_resolve(args) -> int:
    service = _service()
    row = service.attach_outcome(args.prediction_id, int(args.outcome),
                                 source=args.source or "")
    print(json.dumps(row, indent=2, default=str))
    return 0


def main(argv=None) -> int:
    setup_logging()
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    # No subcommand means `run`: the production start command is the bare
    # `python tools/alpha_service_run.py`, so a platform start command needs
    # no arguments and cannot be mistyped into a different mode.
    sub = ap.add_subparsers(dest="cmd", required=False)
    sub.add_parser("health")
    o = sub.add_parser("once")
    o.add_argument("--limit", type=int, default=None)
    r = sub.add_parser("run")
    r.add_argument("--max-cycles", type=int, default=None)
    sub.add_parser("telemetry")
    s = sub.add_parser("resolve")
    s.add_argument("--prediction-id", required=True)
    s.add_argument("--outcome", required=True, choices=["0", "1"])
    s.add_argument("--source", default="")
    args = ap.parse_args(argv)
    if not getattr(args, "cmd", None):
        args = ap.parse_args(["run"])
    try:
        assert_no_broker_credentials()
    except BrokerCredentialsPresent as e:
        # ONE marker line a deployment can grep for, then the reason. Both
        # carry variable NAMES only -- `assert_no_broker_credentials` never
        # reads a value, so there is nothing here that could print one.
        print(STARTUP_REFUSED_MARKER, file=sys.stderr)
        print(str(e), file=sys.stderr)
        return 78
    return {"health": cmd_health, "once": cmd_once, "run": cmd_run,
            "telemetry": cmd_telemetry, "resolve": cmd_resolve}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
