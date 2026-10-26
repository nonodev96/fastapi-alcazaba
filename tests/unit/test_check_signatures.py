"""scripts/check_signatures.py: multi-line signatures end with a comma, one parameter per line."""

import importlib.util
import subprocess
import sys
import textwrap
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).parents[2] / "scripts" / "check_signatures.py"


@pytest.fixture(name="checker", scope="module")
def fixture_checker() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_signatures", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fixed(checker: ModuleType, source: str) -> str:
    source = textwrap.dedent(source)
    result: str = checker.add_commas(source, checker.missing_commas(source))
    compile(result, "fixed", "exec")  # still Python
    return result


class TestMissingCommas:
    def test_a_signature_on_one_line_is_left_alone(self, checker: ModuleType) -> None:
        source = "def f(a: int, b: str = 'x') -> None:\n    pass\n"
        assert checker.missing_commas(source) == []

    def test_parameters_together_on_their_own_line(self, checker: ModuleType) -> None:
        source = """\
            class Test:
                async def test_example(
                    self, caplog: pytest.LogCaptureFixture
                ) -> None:
                    pass
            """
        assert "self, caplog: pytest.LogCaptureFixture,\n" in fixed(checker, source)

    def test_a_trailing_comma_already(self, checker: ModuleType) -> None:
        source = textwrap.dedent("""\
            def f(
                a: int,
                b: int,
            ) -> None:
                pass
            """)
        assert checker.missing_commas(source) == []

    @pytest.mark.parametrize(
        ("parameters", "expected"),
        [
            ("a, *args", "a, *args,"),
            ("a, **kwargs", "a, **kwargs,"),
            ("a, /", "a, /,"),
            ("a, *, b", "a, *, b,"),
            ("a=(1, 2), b=f(x)", "a=(1, 2), b=f(x),"),
            ("a: dict[str, int] = {}", "a: dict[str, int] = {},"),
        ],
    )
    def test_every_kind_of_parameter(
        self,
        checker: ModuleType,
        parameters: str,
        expected: str,
    ) -> None:
        source = f"def f(\n    {parameters}\n) -> None:\n    pass\n"
        assert f"    {expected}\n" in fixed(checker, source)

    def test_a_comment_after_the_last_parameter(self, checker: ModuleType) -> None:
        source = "def f(\n    a: int  # the count\n) -> None:\n    pass\n"
        assert "    a: int,  # the count\n" in fixed(checker, source)

    def test_decorated_and_nested_functions(self, checker: ModuleType) -> None:
        source = """\
            @decorator(
                "x"
            )
            def outer(
                a
            ):
                @wraps(outer)
                async def inner(
                    b, c
                ):
                    pass
            """
        result = fixed(checker, source)
        assert "    a,\n" in result
        assert "        b, c,\n" in result
        assert '    "x"\n' in result  # a call: not a signature

    def test_without_parameters(self, checker: ModuleType) -> None:
        assert checker.missing_commas("def f(\n) -> None:\n    pass\n") == []


class TestCommandLine:
    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        command = [sys.executable, str(SCRIPT), *args]
        return subprocess.run(command, capture_output=True, text=True, check=False)  # noqa: S603

    def test_check_then_fix(self, tmp_path: Path) -> None:
        module = tmp_path / "module.py"
        module.write_text("def f(\n    a, b\n) -> None:\n    pass\n")
        checked = self.run(str(tmp_path))
        assert checked.returncode == 1
        assert f"{module}:2:9: multi-line signature without a trailing comma" in checked.stdout
        assert self.run("--fix", str(module)).returncode == 0
        assert module.read_text() == "def f(\n    a, b,\n) -> None:\n    pass\n"
        assert self.run(str(module)).returncode == 0

    def test_ruff_format_then_puts_one_parameter_per_line(self, tmp_path: Path) -> None:
        module = tmp_path / "module.py"
        module.write_text("async def f(\n    self, caplog: int\n) -> None:\n    pass\n")
        self.run("--fix", str(module))
        ruff = [sys.executable, "-m", "ruff", "format", "--isolated", str(module)]
        subprocess.run(ruff, check=True, capture_output=True)  # noqa: S603
        assert module.read_text() == (
            "async def f(\n    self,\n    caplog: int,\n) -> None:\n    pass\n"
        )
