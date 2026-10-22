"""The filer: Claude Code files Hannah's inbox into her brain, then the vault checks every note it wrote.

Claude Code runs headless in the brain's folder, so the brain's own CLAUDE.md is the filing rules. It is
pinned: one small model, the project's settings only, no MCP servers, no slash commands, and four file
tools. Each tool call prints as it happens. The run fails if Claude Code used any other model, and the
brain is filed only when `Vault.check` finds every note's source and every quote in it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from brain.vault import BRAIN, Vault
from workforce.mission import paint

MODEL = "claude-haiku-5-5"
TOOLS = "Read,Write,Edit,Glob"
PROMPT = "File every file in inbox/ into the brain, following CLAUDE.md. Then update index.md."
# Claude Code reaches for a second, smaller model for titles and summaries unless every slot names one.
PINNED = {
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": MODEL,
    "ANTHROPIC_SMALL_FAST_MODEL": MODEL,
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
}


def command() -> list[str]:
    return [
        "claude", "-p", PROMPT, "--model", MODEL, "--setting-sources", "project", "--strict-mcp-config",
        "--disable-slash-commands", "--tools", TOOLS, "--permission-mode", "acceptEdits",
        "--output-format", "stream-json", "--verbose", "--no-session-persistence",
    ]  # fmt: skip


def step(event: dict, root: Path) -> str | None:
    """One tool call as the room reads it: the tool and the file, relative to the brain."""
    if event.get("type") != "assistant":
        return None
    lines = []
    for part in event.get("message", {}).get("content", []):
        if part.get("type") != "tool_use":
            continue
        args = part.get("input", {})
        where = str(args.get("file_path") or args.get("pattern") or "")
        lines.append(f"{part['name']:<6} {where.removeprefix(str(root) + '/')}")
    return "\n".join(lines) or None


def other_models(result: dict) -> list[str]:
    return sorted(set(result.get("modelUsage", {})) - {MODEL})


def file(root: Path = BRAIN) -> int:
    vault = Vault.seed(root)
    print(paint(f"Filing {len(vault.inbox())} files from the inbox", "1;36"))
    result: dict = {}
    # The command is fixed in this file; nothing in it comes from the inbox or a model.
    with subprocess.Popen(  # noqa: S603
        command(), cwd=root, env=os.environ | PINNED, stdout=subprocess.PIPE, text=True
    ) as run:
        for line in run.stdout or []:
            event = json.loads(line)
            if event.get("type") == "result":
                result = event
            elif said := step(event, root):
                print(said)
    if not result or result.get("is_error"):
        print(paint("Claude Code did not finish the filing", "1;31"))
        return 1
    if extra := other_models(result):
        print(paint(f"Claude Code used a model it was not pinned to: {', '.join(extra)}", "1;31"))
        return 1
    if problems := vault.check():
        for p in problems:
            print(paint(p, "1;31"))
        return 1
    facts = sum(len(vault.note(n).facts) for n in vault.names())
    print(paint(f"{len(vault.names())} notes, {facts} facts, every one found in its source", "1;32"))
    return 0


if __name__ == "__main__":
    sys.exit(file())
