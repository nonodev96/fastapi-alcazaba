"""The `fastapi-alcazaba` command: operations on the app's task queues, and checks of its audit
trail, from a shell.

    fastapi-alcazaba tasks myapp.worker queues
    fastapi-alcazaba tasks myapp.worker:queue dead-letters
    fastapi-alcazaba tasks myapp.worker:queue requeue --all
    fastapi-alcazaba tasks myapp.worker:queue status 3f2a...

The target is `module:attribute`: a `FastAPITaskQueue`, or a function (sync or async) that sets up
what the queue needs (FastAPICache, when the queue uses it: the app's lifespan isn't run) and
returns it. A bare `module` stands for all the queues in it. The current directory is importable,
as with uvicorn (for `tasks` only: `audit` never imports from it). Add `--json` for output that
scripts read.

    fastapi-alcazaba audit verify events.jsonl --key audit-public.pem
    fastapi-alcazaba audit keygen audit-private.pem audit-public.pem

`audit verify` reads the events from JSON lines (with a log line's prefix before the JSON, or
without), checks their signatures, chains and checkpoints, and exits with 1 when something's
wrong. HMAC secrets come from environment variables (`--hmac-env NAME`), never the command line.
"""

import argparse
import asyncio
import dataclasses
import importlib
import inspect
import json
import os
import sys
from collections.abc import Iterator, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi_alcazaba.background.tasks import FastAPITaskQueue

#: Exit status when a task isn't found, or can't be cancelled.
NOT_DONE = 1
#: Exit status of a wrong target or wrong arguments (argparse's).
USAGE = 2


class _UsageError(Exception):
    pass


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command with `argv` (the process's arguments by default); returns its exit status."""
    parser = _parser()
    arguments = parser.parse_args(argv)
    try:
        if arguments.group == "audit":
            # Never from the current directory: it may hold the files being checked, and
            # whoever wrote them could put a module there that the check would import.
            return _audit(arguments)
        if "." not in sys.path and "" not in sys.path:
            sys.path.insert(0, "")  # the app's modules, from the current directory
        return asyncio.run(_tasks(arguments))
    except _UsageError as exc:
        parser.print_usage(sys.stderr)
        print(f"{parser.prog}: error: {exc}", file=sys.stderr)
        return USAGE


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fastapi-alcazaba", description="Operate fastapi-alcazaba from a shell."
    )
    groups = parser.add_subparsers(dest="group", required=True)
    tasks = groups.add_parser("tasks", help="the task queues (FastAPITaskQueue)")
    tasks.add_argument(
        "target",
        help="module:attribute, a queue or a function that returns one; module, all its queues",
    )
    tasks.add_argument("--json", action="store_true", help="print JSON")
    commands = tasks.add_subparsers(dest="command", required=True)
    commands.add_parser("queues", help="each queue: its tasks pending and dead letters")
    commands.add_parser("pending", help="how many tasks haven't finished")
    dead = commands.add_parser("dead-letters", help="the latest dead letters, newest first")
    dead.add_argument("-n", "--count", type=int, default=100, help="how many (100)")
    requeue = commands.add_parser("requeue", help="dead letters back to the queue")
    requeue.add_argument("ids", nargs="*", metavar="ID", help="their IDs")
    requeue.add_argument("--all", action="store_true", help="every dead letter")
    requeue.add_argument(
        "--stale",
        action="store_true",
        help="also those that waited longer than replay_window (look at them first)",
    )
    discard = commands.add_parser("discard", help="delete dead letters")
    discard.add_argument("ids", nargs="+", metavar="ID", help="their IDs")
    status = commands.add_parser("status", help="where a task is")
    status.add_argument("task_id", metavar="TASK_ID")
    cancel = commands.add_parser("cancel", help="make sure a task that hasn't started never runs")
    cancel.add_argument("task_id", metavar="TASK_ID")
    audit = groups.add_parser("audit", help="the audit trail (FastAPIAudit)")
    audit_commands = audit.add_subparsers(dest="command", required=True)
    verify = audit_commands.add_parser("verify", help="check exported events")
    verify.add_argument(
        "files", nargs="+", metavar="FILE", help="JSON lines of events; - for stdin"
    )
    verify.add_argument(
        "--key",
        action="append",
        default=[],
        metavar="PEM",
        help="an Ed25519 public key file (several: every rotation the events span)",
    )
    verify.add_argument(
        "--hmac-env",
        action="append",
        default=[],
        metavar="NAME",
        help="an environment variable holding an HMAC secret",
    )
    verify.add_argument("--json", action="store_true", help="print JSON")
    keygen = audit_commands.add_parser("keygen", help="a new Ed25519 key pair, as PEM files")
    keygen.add_argument("private", metavar="PRIVATE", help="where the private key goes (0600)")
    keygen.add_argument("public", metavar="PUBLIC", help="where the public key goes")
    return parser


async def _tasks(arguments: argparse.Namespace) -> int:
    queues = await _queues(arguments.target)
    if arguments.command == "queues":
        rows = [
            {
                "name": queue.name,
                "pending": await queue.pending(),
                "dead_letters": await queue.dead_letter_count(),
            }
            for queue in queues
        ]
        _print(arguments, rows, ["name", "pending", "dead_letters"])
        return 0
    if len(queues) > 1:
        names = ", ".join(sorted(queue.name for queue in queues))
        raise _UsageError(f"{arguments.target} has several queues ({names}): name one, module:attr")
    queue = queues[0]
    try:
        return await _run(arguments, queue)
    except ValueError as exc:  # a malformed ID
        raise _UsageError(str(exc)) from None


async def _run(arguments: argparse.Namespace, queue: FastAPITaskQueue) -> int:
    command = arguments.command
    if command == "pending":
        _print(arguments, {"pending": await queue.pending()})
    elif command == "dead-letters":
        letters = [
            dataclasses.asdict(letter) for letter in await queue.dead_letters(arguments.count)
        ]
        _print(
            arguments,
            letters,
            ["id", "task", "task_id", "reason", "error", "attempts", "failed_at"],
        )
    elif command == "requeue":
        if arguments.all == bool(arguments.ids):
            raise _UsageError("requeue takes the IDs of dead letters, or --all")
        requeued = await queue.requeue_dead_letters(
            None if arguments.all else arguments.ids, stale=arguments.stale
        )
        _print(arguments, {"requeued": requeued})
    elif command == "discard":
        _print(arguments, {"discarded": await queue.discard_dead_letters(arguments.ids)})
    elif command == "status":
        found = await queue.status(arguments.task_id)
        if found is None:
            print(f"no task {arguments.task_id} (unknown, or ended long ago)", file=sys.stderr)
            return NOT_DONE
        _print(arguments, dataclasses.asdict(found))
    else:  # cancel
        cancelled = await queue.cancel(arguments.task_id)
        _print(arguments, {"cancelled": cancelled})
        if not cancelled:
            print("not cancelled: it's running, finished or unknown", file=sys.stderr)
            return NOT_DONE
    return 0


async def _queues(target: str) -> list[FastAPITaskQueue]:
    """The queues `target` names: `module:attribute` (a queue, or a function returning one), or
    `module` (every queue in it)."""
    module_name, _, attribute = target.partition(":")
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise _UsageError(f"can't import {module_name}: {exc}") from None
    if not attribute:
        found = [value for value in vars(module).values() if isinstance(value, FastAPITaskQueue)]
        if not found:
            raise _UsageError(f"{module_name} has no FastAPITaskQueue: give module:attribute")
        return list({id(queue): queue for queue in found}.values())
    value: Any = module
    for part in attribute.split("."):
        try:
            value = getattr(value, part)
        except AttributeError:
            raise _UsageError(f"{module_name} has no {attribute}") from None
    if not isinstance(value, FastAPITaskQueue) and callable(value):
        value = value()
        if inspect.isawaitable(value):
            value = await value
    if not isinstance(value, FastAPITaskQueue):
        raise _UsageError(f"{target} isn't a FastAPITaskQueue, nor a function returning one")
    return [value]


def _print(
    arguments: argparse.Namespace,
    data: dict[str, Any] | list[dict[str, Any]],
    columns: Sequence[str] = (),
) -> None:
    if arguments.json:
        print(json.dumps(data, default=_json, indent=2))
    elif isinstance(data, dict):
        for name, value in data.items():
            print(f"{name}: {_cell(value)}")
    else:
        _table(data, columns)


def _table(rows: list[dict[str, Any]], columns: Sequence[str]) -> None:
    if not rows:
        print("(none)")
        return
    cells = [[_cell(row[column]) for column in columns] for row in rows]
    widths = [max(len(column), *(len(row[i]) for row in cells)) for i, column in enumerate(columns)]
    print(
        "  ".join(
            column.upper().ljust(width) for column, width in zip(columns, widths, strict=True)
        ).rstrip()
    )
    for row in cells:
        print(
            "  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True)).rstrip()
        )


def _cell(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    return str(value)


def _json(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"{type(value).__name__} isn't JSON")  # pragma: no cover


def _audit(arguments: argparse.Namespace) -> int:
    from fastapi_alcazaba.audit import generate_audit_key_pair, last_checkpoint, verify_audit_trail

    if arguments.command == "keygen":
        private, public = Path(arguments.private), Path(arguments.public)
        if private.exists() or public.exists():
            raise _UsageError("the key files exist already: not overwritten")
        private_pem, public_pem = generate_audit_key_pair()
        descriptor = os.open(private, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="ascii") as file:
            file.write(private_pem)
        public.write_text(public_pem, encoding="ascii")
        print(
            f"private key: {private} (the app's signing_key); public key: {public} (the auditor's)"
        )
        return 0
    keys: list[Any] = []
    for path in arguments.key:
        try:
            keys.append(_public_key(Path(path).read_bytes(), path))
        except OSError as exc:
            raise _UsageError(f"can't read the key {path}: {exc.strerror}") from None
    for name in arguments.hmac_env:
        if not (value := os.environ.get(name)):
            raise _UsageError(f"the environment variable {name} is empty")
        keys.append(value)
    if not keys:
        raise _UsageError("give the keys: --key PEM, or --hmac-env NAME")
    events = list(_events(arguments.files))
    try:
        problems = verify_audit_trail(events, keys)
    except (ValueError, TypeError) as exc:
        raise _UsageError(str(exc)) from None
    checkpoint = last_checkpoint(events)
    report = {
        "events": len(events),
        "streams": len({event.get("stream") for event in events}),
        "last_checkpoint": checkpoint.isoformat() if checkpoint else None,
        "problems": problems,
    }
    if arguments.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"events: {report['events']} in {report['streams']} streams")
        print(f"last checkpoint: {report['last_checkpoint'] or 'none'}")
        print("ok" if not problems else f"{len(problems)} problems:")
        for problem in problems:
            print(f"- {problem}")
    return NOT_DONE if problems else 0


def _public_key(raw: bytes, path: str) -> Any:
    """The Ed25519 key in the file `path`. Never an HMAC secret: a public key taken for one would
    let whoever has it (anybody) sign the events checked with it."""
    from fastapi_alcazaba.audit.keys import audit_keys

    try:
        (key,) = audit_keys(raw)
    except (ValueError, TypeError) as exc:
        raise _UsageError(f"{path}: {exc}") from None
    if key.algorithm != "ed25519":
        raise _UsageError(
            f"{path} isn't an Ed25519 key (PEM, DER or OpenSSH): HMAC secrets go in --hmac-env"
        )
    return key


def _events(files: Sequence[str]) -> Iterator[dict[str, Any]]:
    """The events in `files`: JSON objects with a stream, a sequence and an action, one per line,
    after whatever a log line puts before them; other lines are skipped."""
    for name in files:
        try:
            lines = (
                sys.stdin.readlines()
                if name == "-"
                else Path(name).read_text(encoding="utf-8").splitlines()
            )
        except OSError as exc:
            raise _UsageError(f"can't read {name}: {exc.strerror}") from None
        for line in lines:
            start = line.find("{")
            if start < 0:
                continue
            try:
                found = json.loads(line[start:])
            except ValueError:
                continue
            if isinstance(found, dict) and {"stream", "sequence", "action"} <= found.keys():
                yield found


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
