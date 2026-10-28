# Cut AI Agent Costs with Token Economics

KaaS Insurance's motor claims desk gets claims as photos and a body shop's PDF quote. A claims reader on
KaaS Insurance's board reads each document into a claim file: the damaged parts, how many cars, and the
quote's total. The claims system files it only when the total is the one printed on the quote, and a
claim reads its next document only while it stays inside its budget of $0.003. Only Nina Kowalski, the
adjuster, can raise a claim's budget.

**Before:** every call starts with a timestamp above the desk's long policy, photos go at high detail
and the model reasons at its default effort. The two small claims file; a car hire firm's hail claim,
eight photos and no quote, hits its budget five photos in and waits for Nina. **After:** three levers,
one at a time, each changing what a read costs and nothing about what it reads. All three claims file
inside the same budget and the cost per claim filed falls from $0.0030 to $0.0006.

```
make check     # lint and the acceptance tests: real claims system and ledger, a scripted model
make demo      # one pass of the three claims; PASS=1 is no levers, PASS=4 is all three (default)
make passes    # all four passes, about seven minutes, run before a call
make ladder    # the table below, from the stored passes, no model and no key
make board     # the signed-in board on http://127.0.0.1:8092, serving the last passes
make rehearse  # sign in as Leo and Nina on the running board and walk the hail claim to Done
```

The model key is loaded by `uv run --env-file` from `../mission-control`, never on a command line.

## Three levers

| Lever | What changes | Why it costs less |
| --- | --- | --- |
| Cache the policy | The policy goes first, the same on every call | The provider serves a repeated prefix from its cache at a tenth of the input price |
| Photos at low detail | Each photo is sent at low detail | A damage photo is judged on the whole car, and low detail is about a third of the tokens |
| Think less | The model reasons at low effort | Reasoning tokens bill as output, at five times the input price |

One `make passes` run on 28 Oct 2026, every number from the provider's own token counts:

| Pass | Filed | Fresh input | Cached input | Output | Per claim filed | Per 1,000 claims |
| --- | --- | --- | --- | --- | --- | --- |
| No levers | 2 of 3 | 42,246 | 0 | 3,403 | $0.0030 | $2.96 |
| Cache the policy | 3 of 3 | 14,038 | 53,694 | 4,412 | $0.0014 | $1.38 |
| Photos at low detail | 3 of 3 | 4,131 | 32,059 | 4,124 | $0.0009 | $0.93 |
| Think less | 3 of 3 | 5,778 | 30,603 | 1,881 | $0.0006 | $0.61 |

Output varies run to run, so a lever's saving shows in its token column before it shows in dollars.

## How the card closes

`reader.settle` asks the claims system. A claim stopped by its budget goes to Needs you with how many
documents were read. The card page shows Approve and Decline for the person signed in. Leo Grant, the
claims handler, presses Approve and the claims system refuses him and logs why. Nina signs in and
approves: that one claim's budget rises to $0.01, and it resumes from the document it stopped at, with
the claim file it already had. A decline sends it to Backlog for an assessor. A total that is not the
quote's, or a total on a claim with no quote, is refused. A filed claim is Done.

## What is in here

- `claims/system.py`: KaaS Insurance's claims system: claims, the ledger, the budget, who raises it and the filing rules.
- `claims/reader.py`: the reader, the three levers, `settle` and the ladder.
- `claims/board.py`: the signed-in board with the ladder, the claim file, the ledger and the budget decision.
- `fixtures/`: the claims, the desk's policy, the photos (see `CREDITS.txt`), two quotes and the two people who sign in.

The board, its cards and the model come from [`../mission-control`](../mission-control/). Prices are
set in the `Makefile`, cached input at its own rate.
