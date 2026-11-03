"""The `fastapi-alcazaba` command: operations on the app's task queues from a shell.

    fastapi-alcazaba tasks myapp.worker queues
    fastapi-alcazaba tasks myapp.worker:queue dead-letters
    fastapi-alcazaba tasks myapp.worker:queue requeue --all
    fastapi-alcazaba tasks myapp.worker:queue status 3f2a...

The target is `module:attribute`: a `FastAPITaskQueue`, or a function (sync or async) that sets up
what the queue needs (FastAPICache, when the queue uses it: the app's lifespan isn't run) and
returns it. A bare `module` stands for all the queues in it. The current directory is importable,
as with uvicorn. Add `--json` for output that scripts read.
"""

import argparse
import asyncio
import dataclasses
import importlib
import inspect
import json
import sys
from collections.abc import Sequence
from datetime import datetime
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
    if "." not in sys.path and "" not in sys.path:
        sys.path.insert(0, "")  # the app's modules, from the current directory
    try:
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
    discard = commands.add_parser("discard", help="delete dead letters")
    discard.add_argument("ids", nargs="+", metavar="ID", help="their IDs")
    status = commands.add_parser("status", help="where a task is")
    status.add_argument("task_id", metavar="TASK_ID")
    cancel = commands.add_parser("cancel", help="make sure a task that hasn't started never runs")
    cancel.add_argument("task_id", metavar="TASK_ID")
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
        requeued = await queue.requeue_dead_letters(None if arguments.all else arguments.ids)
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


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
