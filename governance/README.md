# Build an AI Agent Governance Framework

KaaS Hotels runs four hotels in Norway, from Oslo to Stavanger. Four AI employees work their week
before Christmas: the Rate manager prices each night against demand and the competitors, the Supplies
buyer orders stock back to par, the Rota planner covers housekeeping shifts, and the Linen forecaster
runs in shadow. Marta Berg, the operations director, owns one policy file that says what each may do
alone, who signs above that, and what each may spend on AI in a month. The metric is every write to a
hotel's systems with exactly one decision behind it, and a month's page she can read in a minute.

**Before:** each agent's limits live in its prompt. A model that reads the prompt loosely drops a price
under the floor, orders from a supplier nobody approved, or gives a housekeeper eight hours' rest, and
nobody can say afterwards who allowed it (`make naive` shows it, offline). **After:** a gate reads the
policy before any write reaches a system of record. It allows, refuses with the rule it cites, or holds
the action for the person the policy names. That person signs in on the board and approves or declines;
anyone else, Marta included, is refused and the refusal is logged. Every verdict is appended to the
decision record, and `make decisions` checks every write in the hotels' systems against it.

```
make check      # lint and the acceptance tests: real tools, gate, systems and record; scripted model
make naive      # the rate job with the gate off, offline: a price under the floor is written
make demo       # seven jobs from a fresh board, each worked under the policy, every verdict printed
make decisions  # every write in the hotels' systems against the decision record
make board      # the signed-in board, Team and Month on http://127.0.0.1:8096
make rehearse   # sign in as Marta, Ingrid and Lars on the running board and answer the held writes
make record CLIP=board   # record a deck clip (stop make board first)
```

Keys and the sign-in passphrases come from mission-control's env file
(`BOARD_PASSPHRASE_MARTA_BERG`, `BOARD_PASSPHRASE_INGRID_DAHL`, `BOARD_PASSPHRASE_LARS_EIDE`).

## The seven jobs

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

## The decisions on the board

With `make demo` done and `make board` running, J-02, J-04 and J-06 wait in Needs you.
- Marta signs in and approves J-02: refused, "only Ingrid Dahl signs a rate change over 15%". The
  refusal is in the record and on the card, and Bergen's rate is still EUR 165.
- Marta declines J-04: the card is Done, "declined by Marta Berg; nothing was written".
- Ingrid Dahl signs in and approves J-02: the rate system holds EUR 119.
- Lars Eide signs in and approves Aisha's shift on J-06.

`make naive` is the same rate job with the gate off: the prompt says never under the floor, the model
asks for EUR 89, and the rate system holds EUR 89 with no decision behind it.

## What holds the line

- The gate (`operating/crew.py`) is the only path from a tool to a system. A refusal comes back to the
  employee as the rule's text, so it can try again inside it; nothing is written.
- A signature is the person signed in on the board. The action waits in the state folder, untouched in
  the systems, until the named signer approves it or declines it. A decline is never written.
- The policy decides whether an employee starts at all: paused, or its month's AI budget spent.
- The record is appended, never rewritten; the board's card page and Month page read from it.

## Where things live

- `fixtures/`: the policy file, the four hotels and their suppliers, the week's seven jobs, and the three people.
- `operating/policy.py`: every rule, read from the policy file, with the verdict it gives.
- `operating/hotels.py`: the rate, purchasing and rota systems.
- `operating/crew.py`: the employees, their tools and prompts, and the gate.
- `operating/scripted.py`: the offline model behind `make naive`.
- `operating/record.py`: the decision record.
- `operating/run.py` and `operating/board.py`: the week, signing, `make decisions`, and the signed-in board.
