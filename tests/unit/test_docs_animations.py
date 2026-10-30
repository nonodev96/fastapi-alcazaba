"""scripts/docs_animations.py: the examples' animations, up to date and on their pages."""

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).parents[2]
SCRIPT = ROOT / "scripts" / "docs_animations.py"


@pytest.fixture(name="animations", scope="module")
def fixture_animations() -> ModuleType:
    spec = importlib.util.spec_from_file_location("docs_animations", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_files_are_what_the_script_draws(animations: ModuleType) -> None:
    """After changing an animation: python scripts/docs_animations.py"""
    stale = [
        name
        for name, animation in animations.ANIMATIONS.items()
        if (animations.OUT / f"{name}.svg").read_text(encoding="utf-8")
        != animations.render(name, animation)
    ]
    assert stale == []


def test_every_example_starts_with_its_animation() -> None:
    examples = ROOT / "docs" / "examples"
    missing = [
        page.relative_to(ROOT).as_posix()
        for page in examples.rglob("*.md")
        if page != examples / "index.md"
        if '--8<-- "docs/examples/animations/' not in page.read_text(encoding="utf-8")
    ]
    assert missing == []


def test_a_label_too_long_for_its_arrow(animations: ModuleType) -> None:
    animation = animations.Animation(
        "Too long",
        "What it's for: nothing.",
        ("A", "B", "C"),
        (animations.request(0, 1, "a label far too long to fit between two lanes"),),
        "The end",
    )
    with pytest.raises(ValueError, match="too long for its arrow"):
        animations.render("too-long", animation)


def test_a_note_wider_than_the_picture(animations: ModuleType) -> None:
    animation = animations.Animation(
        "Too wide",
        "What it's for: nothing.",
        ("A", "B", "C"),
        (animations.Note(2, "a note too wide for the last lane"),),
        "The end",
    )
    with pytest.raises(ValueError, match="wider than the picture"):
        animations.render("too-wide", animation)
