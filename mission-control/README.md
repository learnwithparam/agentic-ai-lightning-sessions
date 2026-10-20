# Mission Control

AI employees that resolve Acme's customer billing issues. A refund waits for a human on Telegram, no
AI employee can approve one, and a card reaches Done only when the payments ledger agrees. The board
shows what every outcome cost, failed runs included.

**Before:** an agent says "Refunded the customer. All done." and nobody checks; the double charge is
still on the ledger. **After:** that card stays in Backlog with the ledger's reason, and the one that
reaches Done carries the ledger rows that prove it.

```
make check                         # lint and every step's test, offline: no keys, no spend
cp .env.example .env               # then fill in the model key, prices and Telegram bot
make demo FRESH=1                  # work the card; approve on Telegram, or in the terminal without a bot
make board                         # the board on http://127.0.0.1:8080
```

## How a card closes

A customer was charged twice at checkout. The manager hands the card to the support resolver, which
finds the second charge on the same order and requests a refund. A refund cannot be undone, so the
card moves to Needs you and asks on Telegram. The approval rule is code (`mission.decide`), not a
prompt: the manager tries to approve and is refused. After your tap, the card reaches Done only if the
ledger shows one charge per order (`mission.settle`). Cost per outcome is every run's cost divided by
the cards in Done.

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
- `tests/`: one acceptance test per built step, and `test_steps.py`, which fails when a built step
  has none.
