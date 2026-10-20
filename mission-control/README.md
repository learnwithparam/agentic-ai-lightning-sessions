# Mission Control

AI employees that resolve Acme's customer billing issues. A refund waits for a human on Telegram, no
AI employee can approve one, and a card reaches Done only when the payments ledger agrees. The board
shows what every outcome cost, failed runs included.

**Before:** an agent says "Refunded the customer. All done." and nobody checks; the double charge is
still on the ledger. **After:** that card stays in Backlog with the ledger's reason, and the one that
reaches Done carries the ledger rows that prove it.

```
make check                         # lint and every step's test, offline: no keys, no spend
cp .env.example .env               # then fill in the OpenRouter key, prices and Telegram bot
make demo FRESH=1                  # work the board; approve the refund on Telegram
make demo FRESH=1 ASK=terminal     # the same, approving in this terminal
make board                         # the board on http://127.0.0.1:8080
```

## How a card closes

A customer was charged twice at checkout. The manager hands the card to the support resolver, which
finds the second charge on the same order and requests a refund. A refund cannot be undone, so the
card moves to Needs you and asks on Telegram. The approval rule is code (`mission.decide`), not a
prompt: the manager tries to approve and is refused. After your tap, the card reaches Done only if the
ledger shows one charge per order (`mission.settle`).

A second card asks whether the double charge is a known bug and who owns the fix. The manager hands it
to the engineering analyst, which searches GitHub, Linear and the docs. It reaches Done only when the
answer cites a source the analyst read in that run (`mission.answer`). Cost per outcome is every run's cost divided by
the cards in Done.

## Deploy it: Keycloak, OpenFGA, Langfuse

The deploy workshop runs the same board behind three services from `compose.yaml`:

- **Keycloak** signs each AI employee in as itself, acting for one person. The person signs in once.
  Each employee exchanges that token for its own, and Keycloak's introspection names both the employee
  and the person. The board trusts that answer, never what a model says about itself.
- **OpenFGA** decides every tool call and every approval from `deploy/policy.json`. The support
  resolver works the billing desk and the analyst works engineering. sam, a contractor, is staff on
  engineering only. Approving a refund needs a billing approver who is also billing staff, so sam's tap
  is refused even though sam is listed as an approver.
- **Langfuse** keeps every run. Each model call carries its tokens and cost, and each tool and handoff
  is recorded. The card id is the session, so every run on one card reads as one thread.

```
make stack                                   # Langfuse: http://127.0.0.1:8270, billing@acme.example / acme-local
STACK=1 ASK=terminal FRESH=1 make demo       # signs in as param; MISSION_USER=sam to watch billing refuse
make stack-check                             # the promise as a test, against the running stack
```

With the stack on, an approver counts only when `approvers` in `deploy/policy.json` maps their channel
id to an OpenFGA user who can approve, such as `terminal:param`. Add a Telegram tapper by numeric id:
`MISSION_APPROVERS=telegram:123456789=param`. A Telegram username is never used, since anyone can take one. Without `STACK=1` the board runs exactly as in the Lightning session.

## Built in earlier sessions

**A Slack AI agent with memory.** Tag the engineering analyst in a Slack thread. It searches GitHub,
Linear and the docs, replies citing each source, and remembers the thread. A citation to anything it
did not read in that run is removed before it reaches Slack.

```
make ask Q="is the double charge known?"
make slack                         # the analyst in your workspace, over Socket Mode
```

**A company brain with permissions.** Every issue, ticket and doc carries the groups that may read it,
and every search runs as the user who asked. The restricted text never reaches the model for a
contractor. Without `GITHUB_REPO` and `DOCS_DIR` it reads the `fixtures/acme` tenant.

```
make ask Q="why are customers charged twice?" AS=U_CONTRACTOR
```

## Layout

- `workforce/mission.py`: the manager, the support resolver and the maker-checker gate.
- `workforce/board.py`, `workforce/ledger.py`, `workforce/telegram.py`: the board, the payments ledger
  it is checked against, and the approval.
- `web/`: the board's styles and font.
- `workforce/sources.py`, `workforce/analyst.py`, `workforce/slack_app.py`: the readers, the analyst
  and its citation check, and the Slack handler.
- `workforce/access.py`, `workforce/traces.py`, `compose.yaml`, `deploy/`: the deploy step, Keycloak
  and OpenFGA in front of every tool, and the Langfuse exporter.
- `tests/`: one acceptance test per built step, and `test_steps.py`, which fails when a built step
  has none.
