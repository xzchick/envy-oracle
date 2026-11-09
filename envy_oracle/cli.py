"""Command-line entry point. The Oracle window is the default.

Score reads are read-only; every mutation (adding a prediction, resolving one,
removing one) writes a brand-new oracle revision and never overwrites the previous.
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys

from . import __version__
from .core import (OracleError, add_prediction, check_passphrase, create, due, export,
                   inspect, remove_prediction, resolve, score, tags)


def build_parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="envy-oracle",
        description="Encrypted forecasts and calibration scoring — hold yourself to your predictions. Record "
                    "a statement with a confidence, resolve it yes or no later, and see how well your stated "
                    "confidence matched reality. Run with no arguments to open the window. Local only.")
    root.add_argument("--version", action="version", version=f"envy-oracle {__version__}")
    commands = root.add_subparsers(dest="command")
    commands.add_parser("gui", help="open the Oracle window (default)")

    new = commands.add_parser("init", help="create a new, empty encrypted oracle")
    new.add_argument("destination", help="a new oracle file; never overwritten")
    new.add_argument("--title", required=True, help="a name for this oracle")

    add = commands.add_parser("add", help="add a prediction (writes a new revision)")
    add.add_argument("oracle")
    add.add_argument("destination", help="a new oracle file; never overwritten")
    add.add_argument("--statement", required=True, help="what you predict will happen")
    add.add_argument("--confidence", type=int, required=True, metavar="PCT", help="1–99 percent")
    add.add_argument("--due", default=None, metavar="UTC", help="optional resolve-by time (ISO 8601 UTC)")
    add.add_argument("--tag", action="append", default=None, metavar="LABEL",
                     help="a tag for this prediction; repeat for several")

    res = commands.add_parser("resolve", help="resolve a prediction yes or no (writes a new revision)")
    res.add_argument("oracle")
    res.add_argument("destination", help="a new oracle file; never overwritten")
    res.add_argument("--prediction", required=True, help="the prediction id")
    outcome = res.add_mutually_exclusive_group(required=True)
    outcome.add_argument("--yes", dest="outcome", action="store_true", help="it happened")
    outcome.add_argument("--no", dest="outcome", action="store_false", help="it did not")
    res.add_argument("--note", default="", help="an optional note")

    rm = commands.add_parser("remove", help="remove a prediction; its journal events are kept")
    rm.add_argument("oracle")
    rm.add_argument("destination", help="a new oracle file; never overwritten")
    rm.add_argument("--prediction", required=True)

    sc = commands.add_parser("score", help="show calibration (read-only; exit 1 if any are overdue)")
    sc.add_argument("oracle")
    sc.add_argument("--tag", default=None, metavar="LABEL", help="only score predictions with this tag")

    dq = commands.add_parser("due", help="list open predictions with a resolve-by date (read-only; exit 1 if overdue)")
    dq.add_argument("oracle")
    dq.add_argument("--within", type=int, default=None, metavar="DAYS",
                    help="only those due within this many days (overdue always shown)")

    tg = commands.add_parser("tags", help="list the tags in use and how many predictions carry each")
    tg.add_argument("oracle")

    insp = commands.add_parser("inspect", help="print the decrypted oracle and journal (read-only)")
    insp.add_argument("oracle")

    exp = commands.add_parser("export", help="write a plaintext HTML or JSON calibration report")
    exp.add_argument("oracle")
    exp.add_argument("destination", help="a new report file; never overwritten")
    exp.add_argument("--format", default="html", choices=("html", "json", "md"))
    return root


def _passphrase(confirm: bool = False) -> bytes:
    if not sys.stdin.isatty():
        raise OracleError("Open this action in a terminal so the passphrase is not echoed")
    value = getpass.getpass("Oracle passphrase: ").encode()
    if confirm and value != getpass.getpass("Repeat passphrase: ").encode():
        raise OracleError("Passphrases do not match")
    return check_passphrase(value)


def _run(args) -> int:
    if args.command == "init":
        result = create(args.destination, args.title, _passphrase(confirm=True))
        print(f"oracle created → {result['output']} (revision 0)")
        return 0
    if args.command == "add":
        result = add_prediction(args.oracle, args.destination, _passphrase(), args.statement,
                                args.confidence, args.due, args.tag)
        print(f"prediction added {result['prediction_id']} → {result['output']} (revision {result['revision']})")
        return 0
    if args.command == "resolve":
        result = resolve(args.oracle, args.destination, _passphrase(), args.prediction, args.outcome, args.note)
        print(f"resolved {result['resolved']} as {'yes' if result['outcome'] else 'no'} → {result['output']} "
              f"(revision {result['revision']})")
        return 0
    if args.command == "remove":
        result = remove_prediction(args.oracle, args.destination, _passphrase(), args.prediction)
        print(f"removed {result['removed']} → {result['output']} (revision {result['revision']})")
        return 0
    if args.command == "score":
        rep = score(args.oracle, _passphrase(), tag=args.tag)
        if args.tag:
            print(f"tag “{args.tag}” · {rep['total']} prediction(s)")
        if rep["brier"] is None:
            print("No resolved predictions yet — nothing to score.")
        else:
            print(f"{rep['resolved']} resolved · {rep['accuracy_pct']}% accurate · "
                  f"Brier {rep['brier']} (0.25 = always 50%)")
            for b in rep["bins"]:
                print(f"  {b['band']:>7}: {b['count']} preds · you said {b['you_said']}% · "
                      f"actual {b['actual']}% · gap {b['gap']:+}")
        print(f"{rep['open']} open ({rep['overdue']} overdue)")
        return 0 if rep["overdue"] == 0 else 1
    if args.command == "due":
        rep = due(args.oracle, _passphrase(), within_days=args.within)
        if not rep["due"]:
            print("No open predictions with a due date." + (f" (within {args.within}d)" if args.within is not None else ""))
        for r in rep["due"]:
            when = f"{-r['days']}d overdue" if r["overdue"] else f"in {r['days']}d"
            print(f"  [{when:>12}] {r['confidence']}%  {r['statement'][:70]}  (due {r['due_utc']})")
        print(f"{rep['count']} due-dated, {rep['overdue']} overdue")
        return 0 if rep["overdue"] == 0 else 1
    if args.command == "tags":
        result = tags(args.oracle, _passphrase())
        if not result["tags"]:
            print("No tags yet. Add a prediction with --tag.")
        for row in result["tags"]:
            print(f"  {row['tag']:<24} {row['count']}")
        return 0
    if args.command == "export":
        result = export(args.oracle, args.destination, _passphrase(), args.format)
        print(f"export ({result['format']}): wrote {result['output']}  sha256 {result['sha256']}")
        return 0
    if args.command == "inspect":
        print(json.dumps(inspect(args.oracle, _passphrase()), indent=2, ensure_ascii=False))
        return 0
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command in (None, "gui"):
        from .gui import run_app
        return run_app()
    try:
        return _run(args)
    except OracleError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
