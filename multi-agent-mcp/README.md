# Build a Multi-Agent System with MCP

Priya's 07:40 flight to Berlin was cancelled, and she meets a client at 14:00. A trip planner, a booker
and a messenger rebook her on Acme's board. Each one reaches Acme's travel system only through its own
MCP gateway, which lists only that employee's tools and logs every call, refused ones included. The card
reaches Done only when the travel system holds a ticket that lands in time and Priya and the client were
both sent its flight number.

**Before:** one agent holds every tool, fare approval included. Only its judgment stands between Priya
and a £690 business seat it approves itself, over the £400 cap. **After:** each employee holds only its
own tools, and the travel system refuses any AI approver. When the solo agent does book that seat and
approve it (`tests/test_crew.py` makes it), the approval is refused and the card waits in Needs you for
Priya's manager. The crew's card closes with an economy ticket that lands at 11:55 and two people told.

```
make check     # lint and the acceptance tests: real gateways and travel system, a scripted model
make demo      # the crew rebooks Priya with a real model (key from ../mission-control/.env)
make solo      # one agent with every tool works the same card, for comparison
make board     # the board from the last make demo on http://127.0.0.1:8090
```

## How the card closes

The planner reads the trip and the flights on sale, then picks the cheapest one with a seat that lands at
least 90 minutes before the meeting. It hands the card to the booker, which books it. The travel system
checks the rules itself: a flight that lands too late or is full is refused, and a fare over the cap waits
for Dana Ruiz, Priya's manager. The booker hands the card to the messenger, which tells Priya and Klaus
Bergmann the flight and landing time. `team.settle` asks the travel system, never the agents.

| Employee | Gateway lists | Must never |
| --- | --- | --- |
| Trip planner | read_trip, search_flights | Book anything |
| Booker | read_trip, book_flight | Approve a fare over the cap |
| Messenger | read_trip, read_ticket, send_message | Message anyone who is not on the trip |

The gateway (`crew/gateway.py`) is one process per employee. It knows who it serves from how it was
started, not from anything the model says, and it inherits no keys from the run.

## What is in here

- `crew/gateway.py`: the MCP gateway, its scopes and its call log.
- `crew/travel.py`: Acme's travel system: the trip, flights, tickets, approvals and messages.
- `crew/team.py`: the three employees, the solo agent and `settle`.
- `crew/board.py`: Mission Control's board with the ticket and the call log on the card.
- `fixtures/travel.json`: the cancelled trip and the flights on sale, copied fresh on every run.

The board, its cards and the model come from [`../mission-control`](../mission-control/). The travel
system is a local file standing in for a booking API; the rules the card settles on are in its code.
