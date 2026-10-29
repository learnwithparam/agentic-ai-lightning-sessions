# Trace AI Agents in Production

KaaS Dairy's Line 2 runs six machines, from the filler to the palletiser. Each alert from a sensor
goes to two AI employees: Alert triage reads the sensor and joins the alert to an open repair order,
dismisses it when the reading has cleared, or hands it on; the Repair planner looks up the maintenance
manual and books one repair with its procedure and a priority. Ingrid Halvorsen, the maintenance lead,
owns the shift's AI budget. The metric is repair orders per alert, which must be one, and every cent
the crew spent accounted for, killed runs included.

**Before:** the worker is killed mid-run by a deploy. Its trace was to be posted when the run finished,
so Langfuse holds nothing of it; nobody knows what that run spent, and a naive retry books the
compressor's repair twice (`make naive` shows it, offline). **After:** every span reaches Langfuse as it
starts and as it ends. The board moves a card to In flight before its run starts, so a restart finds the
run that died, reads its spend back from its trace, and works the card again; the maintenance system
hands back the order already booked for that alert. `make traces` checks every card's runs against
Langfuse, per employee. A shift that crosses its budget stops and puts a card on the board; only Ingrid,
signed in, can continue it.

```
make check     # lint and the acceptance tests: real tools, tracer and budget; scripted model, fake Langfuse
make naive     # the shift without the fixes, offline: a trace posted at the end, a blind retry
make shift     # the early shift from a fresh board; the worker is killed once A-04's order is written
make resume    # the worker again: the killed run recorded from its trace, the shift finished
make traces    # every card's runs against its traces in Langfuse, per employee
make board     # the signed-in board, spend by employee and each order on http://127.0.0.1:8095
make rehearse  # sign in as Erik and Ingrid on the running board and walk the paused shift to Done
make record CLIP=board   # record a deck clip (stop make board first)
```

Keys and the sign-in passphrases come from mission-control's env file
(`BOARD_PASSPHRASE_INGRID_HALVORSEN`, `BOARD_PASSPHRASE_ERIK_LUND`). Start Langfuse with
`make -C ../mission-control stack`; without it the shift still runs, and a killed run is recorded with
its spend unknown.

## The decision on the board

`make shift BUDGET_USD=0.0006 KILL_AT=none` runs until the crew's spend passes the budget (after A-03),
then pauses and creates card P-1, "Continue the shift past its budget?", in Needs you. Erik Lund, the
plant manager, signs in and presses Continue: the maintenance system's rule refuses him and the refusal
is in the card's log. Ingrid Halvorsen signs in and continues: P-1 moves to Done and only the paused
alerts, A-04 to A-07, run. A second press finds no paused shift and changes nothing.

## The early shift

| Alert | Machine | What should happen |
| --- | --- | --- |
| A-01 | Filler F2, pump bearing vibration over its limit | Booked, high |
| A-02 | Pasteuriser P1, holding tube below 72.0 °C | Booked, critical |
| A-03 | Capper C3, 14 torque faults in an hour | Booked, medium |
| A-04 | Ammonia compressor K1, oil pressure below 2.0 bar | Booked, high; the worker is killed right after |
| A-05 | Filler F2 again, the same fault | Joined to A-01's order |
| A-06 | Labeller L1, photo-eye blocked for 40 seconds | Dismissed, the sensor reads 0 again |
| A-07 | Palletiser R1, servo motor over its limit | Booked, medium |

Five orders, WO-2001 to WO-2005, and every alert held by exactly one of them or dismissed.

## What holds the line

- The maintenance system (`upkeep/plant.py`) keys every booking on the alert id, which code passes and
  is never a tool argument. A second booking for the same alert returns the order already there; a
  second alert for the same procedure on a machine with an open order joins it. It refuses a procedure
  the manual does not list.
- The board is the checkpoint: In flight is written before the run, Done only once the maintenance
  system holds the outcome.
- A killed run is a failed run on its card, priced from its trace's tokens with the same price list
  as every other run (`upkeep/cost.py`).
- Who continues a paused shift is the person signed in, never a form field; `run.allow_continue`
  refuses anyone but the maintenance lead and logs the refusal before it raises.

## Where things live

- `fixtures/`: the plant, its sensors and manual, the shift's seven alerts, and the two people.
- `upkeep/crew.py`: the two employees, their tools and prompts, one run per alert.
- `upkeep/stream.py`: the tracer that posts each span as it starts and ends.
- `upkeep/ledger.py`: a card's runs read back from Langfuse's observations API, tokens per employee.
- `upkeep/run.py`: the shift, the kill, the pause, the recovery and `make traces`.
- `upkeep/naive.py` and `upkeep/scripted.py`: the offline demo and the scripted crew model.
- `upkeep/budget.py` and `upkeep/board.py`: the budget and its decisions, and the signed-in board.
