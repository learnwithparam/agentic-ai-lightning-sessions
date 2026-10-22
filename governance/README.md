# Build an AI Agent Governance Framework

Halden Hotels runs four hotels in Norway, from Oslo to Stavanger. Four AI employees work their week
before Christmas: the Rate manager prices each night against demand and the competitors, the Supplies
buyer orders stock back to par, the Rota planner covers housekeeping shifts, and the Linen forecaster
runs in shadow. Marta Berg, the operations director, owns one policy file that says what each may do
alone, who signs above that, and what each may spend on AI in a month. The metric is every write to a
hotel's systems with exactly one decision behind it, and a month's page she can read in a minute.

**Before:** each agent's limits live in its prompt. A model that reads the prompt loosely drops a price
under the floor, orders from a supplier nobody approved, or gives a housekeeper eight hours' rest, and
nobody can say afterwards who allowed it. **After:** a gate reads the policy before any write reaches a
system of record. It allows, refuses with the rule it cites, or holds the action for the person the
policy names, who approves or declines it on the board. Every verdict is appended to the decision
record, and `make record` checks every write in the hotels' systems against it.

```
make check     # lint and the acceptance tests: real tools, gate, systems and record; scripted model
make week      # week 51 from a fresh board: each job worked under the policy, every verdict printed
make record    # every write in the hotels' systems against the decision record
make board     # the board with Approve and Decline, Team and Month on http://127.0.0.1:8096
```

Keys come from mission-control's env file.

## Week 51

| Job | Employee, hotel | What should happen |
| --- | --- | --- |
| J-01 | Rate manager, Oslo | EUR 149 to 139, allowed: under 15% |
| J-02 | Rate manager, Bergen | EUR 165 to 119, asks Ingrid Dahl: a 28% drop |
| J-03 | Supplies buyer, Oslo | 120 king sheets from Nordlin, EUR 504, allowed |
| J-04 | Supplies buyer, Bergen | 60 kg coffee, EUR 1,260, asks Marta Berg: over EUR 800 |
| J-05 | Supplies buyer, Trondheim | QuickStock refused, not approved; Fjordvann, EUR 240, allowed |
| J-06 | Rota planner, Stavanger | Jonas refused, 8 h rest; Aisha asks Lars Eide, the hotel manager |
| J-07 | Linen forecaster, Oslo | Not started: paused by Marta Berg |

Three written alone, three waiting for a signature, one never started.

## What holds the line

- The gate (`operating/crew.py`) is the only path from a tool to a system. A refusal comes back to the
  employee as the rule's text, so it can try again inside it; nothing is written.
- A signature is a person on the board. The action waits in `waiting.json`, untouched in the systems,
  until the named signer approves it or declines it.
- The policy decides whether an employee starts at all: paused, or its month's AI budget spent.
- The record is appended, never rewritten; the board's card page and Month page read from it.

## Where things live

- `fixtures/`: the policy file, the four hotels and their suppliers, and the week's seven jobs.
- `operating/policy.py`: every rule, read from the policy file, with the verdict it gives.
- `operating/hotels.py`: the rate, purchasing and rota systems.
- `operating/crew.py`: the employees, their tools and prompts, and the gate.
- `operating/record.py`: the decision record.
- `operating/run.py` and `operating/board.py`: the week, signing, `make record`, and the board pages.
