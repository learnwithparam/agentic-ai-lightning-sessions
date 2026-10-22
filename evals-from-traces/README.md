# Build AI Agent Evals from Production Traces

Quillon Pharma's medical information desk answers doctors and pharmacists about Cardexa, a blood
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

## What one live run showed

On 22 Oct 2026 the production desk passed 11 of 12. The candidate passed 4 and answered all three
side effect reports from the label, so the gate refused it. Its model's tokens cost less, yet each
inquiry cost more: $0.19 per 1,000 against production's $0.15, because it read more tokens per
inquiry and wrote answers about three times as long. A rerun moves the candidate's count by a case or so; the missed side effects
recur, which is why they are a reason on their own.

## Where things live

- `fixtures/`: the approved label, the inquiries, Amara's labels, and her hand grades for the judge.
- `evals/desk.py`: the desk, its four tools, the production prompt and the candidate.
- `evals/testset.py`: traces from Langfuse's public API rebuilt into what each run did, joined to labels.
- `evals/score.py` and `evals/judge.py`: the code checks, the gate, and the judge with its calibration.
- `evals/board.py`: Mission Control's board, one card per case, the swap decision above the lanes.
