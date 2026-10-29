"""Every Lightning in sessions.toml is either built, with a folder whose steps.toml marks its slug built,
or not built and has no folder yet. Every folder with a Makefile is a listed session, so a demo cannot
ship in this repo without a row a reader can find. Every row names the board job, the truth outside the
model, the screen the room watches and what it sells, so no session is planned as code on slides.
AGENTS.md is the one brief every agent reads, and CLAUDE.md only imports it, so the two cannot drift.
A session marked `readable = true` also reads like English on screen: names of three letters or more, no
unpacked triples, no walrus, no log line parsed back with removeprefix."""

import ast
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLAN = ("job", "truth", "screen", "sells")
MIN_NAME_LENGTH = 3


def hard_to_read(source_file: Path, folder: str) -> list[str]:
    where = f"{folder}/{source_file.relative_to(ROOT / folder)}"
    found = []
    for node in ast.walk(ast.parse(source_file.read_text())):
        line = f"{where}:{getattr(node, 'lineno', 0)}"
        names = []
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names = [node.id]
        elif isinstance(node, ast.arg):
            names = [node.arg]
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names = [node.name]
        found += [
            f"{line}: the name {name!r} is too short to read; use {MIN_NAME_LENGTH}+ letters"
            for name in names
            if len(name) < MIN_NAME_LENGTH and name != "_"
        ]
        if isinstance(node, ast.NamedExpr):
            found.append(f"{line}: a walrus hides a step; assign on its own line")
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Tuple) and len(target.elts) >= 3 for target in node.targets
        ):
            found.append(f"{line}: unpacking three or more values at once; name each on its own line")
        is_removeprefix = isinstance(node, ast.Attribute) and node.attr == "removeprefix"
        if is_removeprefix:
            found.append(f"{line}: removeprefix parses a string back; keep a record with fields instead")
    return found


def unknown_targets(folder: Path) -> list[str]:
    """Every `make target` a session doc tells a reader to run must be a target of that Makefile."""
    targets = set(re.findall(r"^([A-Za-z][\w-]*):", (folder / "Makefile").read_text(), re.M))
    out = []
    for doc in ("README.md", "AGENTS.md"):
        if (folder / doc).is_file():
            for name in sorted(set(re.findall(r"`make ([a-z][\w-]*)", (folder / doc).read_text()))):
                if name not in targets:
                    out.append(f"{folder.name}/{doc}: `make {name}` is not a target in its Makefile")
    return out


def problems() -> list[str]:
    sessions = tomllib.loads((ROOT / "sessions.toml").read_text())["session"]
    out, folders = [], {s["folder"] for s in sessions}
    for s in sessions:
        d = ROOT / s["folder"]
        out += [f"{s['folder']}: sessions.toml has no {k}" for k in PLAN if not s.get(k, "").strip()]
        if not s["built"]:
            if d.exists():
                out.append(f"{s['folder']}: folder exists but sessions.toml says built = false")
            continue
        if not (d / "Makefile").exists():
            out.append(f"{s['folder']}: built but has no Makefile")
            continue
        out += unknown_targets(d)
        if s.get("readable"):
            for source_file in sorted(d.rglob("*.py")):
                if ".venv" not in source_file.parts:
                    out += hard_to_read(source_file, s["folder"])
        steps = tomllib.loads((d / "steps.toml").read_text())["step"] if (d / "steps.toml").exists() else []
        if not any(st["slug"] == s["slug"] and st["built"] for st in steps):
            out.append(f"{s['folder']}: steps.toml does not mark {s['slug']} built")
    if not (ROOT / "AGENTS.md").is_file():
        out.append("AGENTS.md: missing; every agent starts blind")
    claude = ROOT / "CLAUDE.md"
    if not claude.is_file() or "@AGENTS.md" not in claude.read_text().split():
        out.append("CLAUDE.md: must import @AGENTS.md, so Claude reads the same brief")
    for mk in ROOT.glob("*/Makefile"):
        if mk.parent.name not in folders:
            out.append(f"{mk.parent.name}: a session folder missing from sessions.toml")
    return out


if __name__ == "__main__":
    found = problems()
    print("\n".join(found) or "sessions.toml matches the folders")
    sys.exit(1 if found else 0)
