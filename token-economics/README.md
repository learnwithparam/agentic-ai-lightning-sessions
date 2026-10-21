# Cut AI Agent Costs with Token Economics

Acme Insurance's motor claims desk gets claims as photos and a body shop's PDF quote. A claims reader on
Acme's board reads each document into a claim file: the damaged parts, how many cars, and the quote's
total. Acme's claims system files it only when the total is the one printed on the quote, and a claim
reads its next document only while it stays inside its budget of $0.003. Nina Kowalski, the adjuster,
decides any claim that hits it.

**Before:** every call starts with a timestamp above the desk's long policy, photos go at high detail
and the model reasons at its default effort. The two small claims file; the Northgate hail claim, eight
photos of a hire fleet, hits its budget five photos in and waits for Nina. **After:** three levers,
one at a time, each changing what a read costs and nothing about what it reads. All three claims file
and the cost per claim filed falls from $0.0030 to $0.0006.

```
make check     # lint and the acceptance tests: real claims system and ledger, a scripted model
make demo      # the same three claims four times, one more lever each pass (key from ../mission-control/.env)
make board     # the board from the last make demo on http://127.0.0.1:8092
```

## Three levers

| Lever | What changes | Why it costs less |
| --- | --- | --- |
| Cache the policy | The policy goes first, the same on every call | The provider serves a repeated prefix from its cache at a tenth of the input price |
| Photos at low detail | Each photo is sent at low detail | A damage photo is judged on the whole car; low detail is about a third of the tokens |
| Think less | The model reasons at low effort | Reasoning tokens bill as output, at five times the input price |

One `make demo` run on 22 Oct 2026, every number from the provider's own token counts:

| Pass | Filed | Fresh input | Cached input | Output | Per claim filed | Per 1,000 claims |
| --- | --- | --- | --- | --- | --- | --- |
| No levers | 2 of 3 | 42,327 | 0 | 3,718 | $0.0030 | $3.05 |
| Cache the policy | 3 of 3 | 9,579 | 58,036 | 4,665 | $0.0013 | $1.29 |
| Photos at low detail | 3 of 3 | 3,324 | 32,969 | 5,996 | $0.0012 | $1.22 |
| Think less | 3 of 3 | 3,401 | 33,064 | 2,443 | $0.0006 | $0.63 |

Output varies run to run, so a lever's saving shows in its token column before it shows in dollars.

## How the card closes

`reader.settle` asks the claims system. A claim stopped by its budget goes to Needs you with how many
documents were read. A total that is not the quote's, or a total on a claim with no quote, is refused and
the card goes back to Backlog. A filed claim is Done. The card page shows the claim file and the ledger:
every read, its tokens and its cost, and the read the budget stopped.

## What is in here

- `claims/system.py`: Acme's claims system: claims, the ledger, the budget and the filing rules.
- `claims/reader.py`: the reader, the three levers, `settle` and the ladder.
- `claims/board.py`: Mission Control's board with the ladder, the claim file and the ledger.
- `fixtures/`: the claims, the desk's policy, the photos (see `CREDITS.txt`) and two quotes.

The board, its cards and the model come from [`../mission-control`](../mission-control/). Prices are
set in the `Makefile`, cached input at its own rate.
