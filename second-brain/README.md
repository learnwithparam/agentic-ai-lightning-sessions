# Build a Second Brain with Claude Code

Hannah Moreau is a KaaS Consulting consultant, and her client is a grocery chain with 42 stores across
the West Midlands. Everything she captures lands in one inbox: meeting notes, a voice memo of a call
with the CFO, and the client's Q3 store review as a PDF. Claude Code files the inbox into her brain,
one note per file, with every fact quoted word for word from its source. Then a researcher on KaaS Consulting's
board answers her client's questions from the brain, over MCP, and cites each fact it uses.

**Before:** the same model with no brain says it has no access to Hannah's client work, and asks what
the project is. **After:** the pilot question and the payback question are Done, each answer resting on
a quote the brain checked, and the 2027 IT budget question waits for Hannah, because no note holds it.

Only Hannah answers for the client. Ravi Shah, an associate, signs in on the board, tries to answer the
IT budget card, and the brain refuses him and logs it. Hannah signs in, answers with her source, and
the brain files her answer as a note that the check still passes.

```
make check     # lint and the acceptance tests: real vault and MCP server, a scripted model, no keys
make file      # Claude Code files the inbox into a fresh brain, on your own Claude Code login
make file-offline  # the notes from the last real filing, checked by the vault (no Claude Code, no network)
make demo      # the researcher answers the board's three questions (key from ../mission-control/.env)
make naive     # the same model, the same questions, no brain
make board     # the board from the last make demo on http://127.0.0.1:8093, signed in
make rehearse  # signs in as Ravi and Hannah on the real board and walks the IT budget card
make record CLIP=brain  # records the board clip for the deck
```

## Three checks, in the code

| Check | Where | What it refuses |
| --- | --- | --- |
| Every note against its source | `Vault.check`, after `make file` | A note whose source is not in the inbox, a fact not in its source word for word, an inbox file with no note |
| Every citation against the notes | `Vault.cite`, behind the `cite` tool | A note the brain does not hold, a quote that is not one of the note's facts |
| Three searches per question | `search_notes`, in the brain's server | A fourth search, so a model that finds nothing must cite or say so |

The first two mean an answer that cites the brain cites something Hannah captured, never something a
model wrote. The answer's own wording is the model's; the board shows each cited quote under it.

## How the card closes

`answerer.settle` reads the brain server's call log, never the answer. A card with at least one accepted
citation is Done, answered from the notes it cites. A card whose citations were all refused, or that
cited nothing, goes to Needs you for Hannah. Her answer, filed by `Vault.file_answer`, closes it. The card page shows the answer, each citation linked to its
note, and every call the researcher made to the brain, refusals included. A note page marks the fact
that was cited.

## Filing with Claude Code

`make file` runs Claude Code headless in `.brain/`, so the brain's own `CLAUDE.md` is the filing rules.
It is pinned in `brain/filer.py`: one small model in every model slot, the project's settings only, no
MCP servers, no slash commands, and four file tools. The run fails if Claude Code used any other model,
and the brain is filed only when `Vault.check` passes.

One `make file` run on 22 Oct 2026 filed 4 notes and 25 facts, every one found in its source, the PDF
included.

## What is in here

- `brain/vault.py`: the brain on disk, `check`, `cite` and the keyword search.
- `brain/filer.py`: Claude Code, pinned, filing the inbox, then the vault's check.
- `brain/server.py`: the brain's MCP server: `search_notes`, `read_note` and `cite`, and the call log.
- `brain/answerer.py`: the researcher, `settle`, and the naive comparison.
- `brain/board.py`: Mission Control's board with each answer's citations, the brain's calls and notes.
- `fixtures/inbox/`: Hannah's inbox. `fixtures/brain/CLAUDE.md`: the filing rules.

The board, its cards and the model come from [`../mission-control`](../mission-control/).
