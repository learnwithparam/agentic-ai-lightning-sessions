# Build AI Agent Evals from Production Traces

KaaS Pharma's medical information desk answers doctors about Cardexa, a blood
pressure tablet, from its approved label only. Every inquiry ends in one action: answer and cite the
label's sections, send a side effect or an overdose to the safety team, or decline what the label does
not cover. Dr. Amara Okafor, the medical information lead, has labelled what each of last week's
inquiries should have got. The metric is cases passed on a test set built from the desk's own runs, and
one failure no pass rate buys back: a side effect answered instead of sent to safety.

**Before:** someone wants to swap the desk to another model with a tidier, shorter prompt, and the only
check is reading a few answers by hand. **After:** last week's real runs come back out of Langfuse as a
test set, each joined to Amara's label. Code checks score the route, the citations and every number;
a judge reads the rest, and counts only once it agrees with Amara's own grades. The candidate reruns
the same inquiries, and the gate refuses the swap when it misses a side effect, whatever else it passes.

```
make check     # lint and the acceptance tests: real tools, checks and gate; scripted models, a fake Langfuse
make runs      # a week of inquiries to the production desk, each run traced to Langfuse
make demo      # test set from those traces, production scored, candidate rerun, the swap gated
make board     # the evals and the board from the last make demo on http://127.0.0.1:8094
make rehearse  # sign in as both people on the running board and check the swap decision
```

Keys come from mission-control's env file. With Langfuse up (`make -C ../mission-control stack`) the
test set is read from its traces; without it, from the local run log, and `make demo` says which.

## What scores a case

| Check | Reads | Fails when |
| --- | --- | --- |
| Route | The action tool the run called | It is not the label's; a side effect answered is critical |
| Citations | The answer's section ids | One is not in the label, or the labelled section is missing |
| Numbers | Every number in the answer | It is not printed in a section the answer cites |
| Judge | The answer against its cited text | It misses the question or says what the text does not, and the judge is trusted |

Code checks run first because they are exact and free. The judge reads only the answers they passed.
It grades ten answers Amara graded by hand first, and below eight of ten its verdicts are shown, never
counted.

## How the swap is decided

`score.gate` ships the candidate only if no case has a critical failure and it passes at least as many
cases as production. A missed side effect is a reason on its own, never a weight in an average: one
rash answered from the label is a report the safety team never logged.

## What the live runs showed

On 29 Oct 2026 the production desk passed 11 of 12 and cost $0.157 per 1,000 inquiries. The default
candidate (a shorter prompt on a cheaper model) passed 4 of 12: it never acted on the three side effect
reports and on two more inquiries, so the gate refused it ("passes 4 of 12; production passes 11").

`make demo CANDIDATE=safe` keeps production's prompt, and with it the safety rule, on a cheaper model. It
passed 12 of 12, 12 of 12 and 11 of 12 on three runs, each against production's 10 or 11, and the gate
passed it. A run moves a count by a case or so, because the model and the judge are not deterministic; a
passed swap still waits on the board for Dr. Amara Okafor, who is the only person who can sign it.

```
make reset                       # production goes back to desk.toml
make demo CANDIDATE=safe         # the gate passes the candidate
make board                       # sign in as Jonas Weber (refused), then as Dr. Amara Okafor (signs)
make rehearse                    # drives the board as both and checks seven claims
```

Passphrases are `BOARD_PASSPHRASE_JONAS_WEBER` and `BOARD_PASSPHRASE_AMARA_OKAFOR` in mission-control's
env file, named in `../mission-control/.env.example`.

## Where things live

- `fixtures/`: the approved label, the inquiries, Amara's labels, and her hand grades for the judge.
- `evals/desk.py`: the desk, its four tools, the production prompt and the candidate.
- `evals/testset.py`: traces from Langfuse's public API rebuilt into what each run did, joined to labels.
- `evals/score.py` and `evals/judge.py`: the code checks, the gate, and the judge with its calibration.
- `evals/board.py`: Mission Control's board with sign-in, one card per case, the swap decision above the lanes.
- `evals/production.py`: `switch`, the one place production changes model, and who may sign it.
