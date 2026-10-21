"""Every Lightning in sessions.toml is either built, with a folder whose steps.toml marks its slug built,
or not built and has no folder yet. Every folder with a Makefile is a listed session, so a demo cannot
ship in this repo without a row a reader can find. Every row names the board job, the truth outside the
model, the screen the room watches and what it sells, so no session is planned as code on slides."""

import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLAN = ("job", "truth", "screen", "sells")


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
        steps = tomllib.loads((d / "steps.toml").read_text())["step"] if (d / "steps.toml").exists() else []
        if not any(st["slug"] == s["slug"] and st["built"] for st in steps):
            out.append(f"{s['folder']}: steps.toml does not mark {s['slug']} built")
    for mk in ROOT.glob("*/Makefile"):
        if mk.parent.name not in folders:
            out.append(f"{mk.parent.name}: a session folder missing from sessions.toml")
    return out


if __name__ == "__main__":
    found = problems()
    print("\n".join(found) or "sessions.toml matches the folders")
    sys.exit(1 if found else 0)
