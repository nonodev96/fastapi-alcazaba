"""Multi-line signatures end with a comma, so that ruff format puts one parameter per line.

    python scripts/check_signatures.py fastapi_alcazaba examples tests scripts
    python scripts/check_signatures.py --fix fastapi_alcazaba examples tests scripts

When a signature doesn't fit on one line, ruff format puts its parameters together on one
indented line if they fit there,

    async def test_example(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:

and one per line only when the last one is followed by a comma (its "magic trailing comma").
This finds the signatures whose parentheses are on different lines and whose last parameter
has no comma; --fix adds it, and the next `ruff format` puts each parameter on its own line.
Ruff's own rule for it, COM812, applies to every call and literal too, and its formatter
warns against it.
"""

import argparse
import ast
import bisect
import io
import sys
import tokenize
from collections.abc import Iterator
from pathlib import Path

_SKIPPED = {tokenize.NL, tokenize.NEWLINE, tokenize.COMMENT}


def missing_commas(source: str) -> list[tuple[int, int]]:
    """Where a comma is missing: (line, column) right after the last parameter of each multi-line
    signature without one."""
    tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    starts = [t.start for t in tokens]
    found = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        arguments = node.args
        named = arguments.posonlyargs + arguments.args + arguments.kwonlyargs
        if not named and arguments.vararg is None and arguments.kwarg is None:
            continue
        index = bisect.bisect_left(starts, (node.lineno, node.col_offset))  # `def` or `async`
        opening, closing = _parameter_list(tokens, index, node)
        if tokens[opening].start[0] == tokens[closing].start[0]:
            continue  # on one line
        last = next(t for t in reversed(tokens[opening:closing]) if t.type not in _SKIPPED)
        if last.string != ",":
            found.append(last.end)
    return found


def _parameter_list(
    tokens: list[tokenize.TokenInfo],
    index: int,
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> tuple[int, int]:
    """The indices of the parentheses around the parameters of `node`, whose `def` (or `async`)
    is the token at `index`."""
    while not (tokens[index].type == tokenize.NAME and tokens[index].string == node.name):
        index += 1
    while tokens[index].string != "(":
        index += 1
    depth = 0
    for closing in range(index, len(tokens)):
        depth += {"(": 1, ")": -1}.get(tokens[closing].string, 0)
        if depth == 0:
            return index, closing
    raise ValueError(f"unbalanced parentheses in the signature of {node.name}")


def add_commas(source: str, places: list[tuple[int, int]]) -> str:
    lines = source.splitlines(keepends=True)
    for row, column in sorted(places, reverse=True):
        line = lines[row - 1]
        lines[row - 1] = line[:column] + "," + line[column:]
    return "".join(lines)


def python_files(paths: list[str]) -> Iterator[Path]:
    for name in paths:
        path = Path(name)
        if path.is_dir():
            yield from sorted(path.rglob("*.py"))
        elif path.suffix in (".py", ".pyi"):
            yield path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("paths", nargs="+", help="files and directories to check")
    parser.add_argument("--fix", action="store_true", help="add the missing commas")
    args = parser.parse_args()
    missing = 0
    for path in python_files(args.paths):
        source = path.read_text(encoding="utf-8")
        places = missing_commas(source)
        if not places:
            continue
        missing += len(places)
        if args.fix:
            path.write_text(add_commas(source, places), encoding="utf-8")
            print(f"{path}: added {len(places)} trailing commas")
        else:
            for row, column in places:
                print(f"{path}:{row}:{column + 1}: multi-line signature without a trailing comma")
    if missing and not args.fix:
        print(f"{missing} signatures to fix: run it with --fix, then ruff format", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
