"""Every built step in steps.toml has an acceptance test marked with its slug, and no test names a step
that is not listed. A lab cannot be taught from this repo until its promise is a passing test."""

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STEPS = tomllib.loads((ROOT / "steps.toml").read_text())["step"]
MARKED = {
    m
    for f in (ROOT / "tests").glob("test_*.py")
    for m in re.findall(r'@pytest\.mark\.lab\("([^"]+)"\)', f.read_text())
}


def test_every_built_step_has_an_acceptance_test():
    missing = [s["slug"] for s in STEPS if s["built"] and s["slug"] not in MARKED]
    assert not missing, f"built steps with no @pytest.mark.lab test: {missing}"


def test_every_marked_test_names_a_listed_step():
    listed = {s["slug"] for s in STEPS}
    assert MARKED <= listed, f"tests mark steps missing from steps.toml: {MARKED - listed}"


def test_a_step_is_not_built_without_its_test():
    unbuilt_but_tested = [s["slug"] for s in STEPS if not s["built"] and s["slug"] in MARKED]
    assert not unbuilt_but_tested, f"mark these built in steps.toml: {unbuilt_but_tested}"
