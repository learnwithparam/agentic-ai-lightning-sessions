"""Every built step in steps.toml has an acceptance test marked with its slug, and no test names a step
that is not listed. A lab cannot be taught from this repo until its promise is a passing test."""

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STEPS = tomllib.loads((ROOT / "steps.toml").read_text())["step"]
MARKED = {
    slug
    for test_file in (ROOT / "tests").glob("test_*.py")
    for slug in re.findall(r'@pytest\.mark\.lab\("([^"]+)"\)', test_file.read_text())
}


def test_every_built_step_has_an_acceptance_test():
    missing = [
        step["slug"] for step in STEPS if step["built"] and step["slug"] not in MARKED
    ]
    assert not missing, f"built steps with no @pytest.mark.lab test: {missing}"


def test_every_marked_test_names_a_listed_step():
    listed = {step["slug"] for step in STEPS}
    assert listed >= MARKED, (
        f"tests mark steps missing from steps.toml: {MARKED - listed}"
    )


def test_a_step_is_not_built_without_its_test():
    unbuilt_but_tested = [
        step["slug"] for step in STEPS if not step["built"] and step["slug"] in MARKED
    ]
    assert not unbuilt_but_tested, (
        f"mark these built in steps.toml: {unbuilt_but_tested}"
    )
