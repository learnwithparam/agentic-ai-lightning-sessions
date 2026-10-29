# Mission Control

AI employees that work KaaS's GitHub issues and engineers' questions on one board. A reply to a
customer waits for a signed-in support approver, no AI employee can approve one, and a card reaches
Done only when GitHub shows the reply. The board shows what every outcome cost, failed runs included.

**Before:** an agent says "I replied to the customer on GH#412. All done." and nobody checks; the issue
has no reply (`make naive`). **After:** that card stays in Backlog with GitHub's reason, and the one that
reaches Done carries the comment GitHub shows and who approved it.

```
make check                         # lint and every step's test, offline: no keys, no spend
make naive                         # the card is Done on the agent's word, offline
cp .env.example .env               # then fill in the OpenRouter key, prices and a passphrase each for Param and Sam
make demo FRESH=1                  # work the three cards; two wait for a support approver
make board                         # the board on http://127.0.0.1:8080; sign in as Param or Sam
make rehearse                      # drive the running board as both people and check every claim
make record CLIP=board             # re-record a deck clip (stop make board first)
```

The sign-in needs `BOARD_SECRET`, `BOARD_PASSPHRASE_PARAM` and `BOARD_PASSPHRASE_SAM` in `.env`.

## How a card closes

A customer opened GH#412: the CSV export times out on a 61,000-task workspace. The manager hands the
card to the support resolver, which reads the issue, finds the fix planned in Linear (EXP-41, release
2.14) and the workaround in the docs, and drafts a reply. A draft that cites nothing it read is sent
back. A reply to a customer cannot be unsent, so the card moves to Needs you, showing the draft and what
it rests on.

Who approves comes from the sign-in, never from the form. Sam is a contractor: his approval is refused
("only a support approver can post to a customer") and the refusal is written to the card. Param is a
support approver: after the approval the card reaches Done only if GitHub shows the reply
(`mission.settle`). A decline posts nothing and the card is Done as declined, so the next run does not
ask again; C-3, a refund reply for GH#415, shows it. The customer never sees the internal ids.

A second card asks whether the export timeout is a known bug and who owns the fix. The manager hands
it to the engineering analyst, which searches GitHub, Linear and the docs. It reaches Done only when
the answer cites a source the analyst read in that run (`mission.answer`). Cost per outcome is every
run's cost divided by the cards in Done.

The board has a sign-in page, the board with its Approve and Decline buttons, a card page with every
run step by step, and the team page with what each employee is hired for and must never do.

## Deploy it: Keycloak, OpenFGA, Langfuse

The deploy workshop runs the same board behind three services from `compose.yaml`:

- **Keycloak** signs each AI employee in as itself, acting for one person. The person signs in once.
  Each employee exchanges that token for its own, and Keycloak's introspection names both the employee
  and the person. The board trusts that answer, never what a model says about itself.
- **OpenFGA** decides every tool call and every approval from `deploy/policy.json`. The support
  resolver works the support desk and the analyst works engineering. sam, a contractor, is staff on
  engineering only. Approving a reply needs an approver who is also support staff, and only param is.
- **Langfuse** keeps every run. Each model call carries its tokens and cost, and each tool and handoff
  is recorded. The card id is the session, so every run on one card reads as one thread.

```
make stack                                   # Langfuse: http://127.0.0.1:8270, support@kaas.example / kaas-local
STACK=1 make demo FRESH=1                    # signs in as param; MISSION_USER=sam to watch support refuse
make stack-check                             # the promise as a test, against the running stack
```

With the stack on, the board asks OpenFGA whether the signed-in person can approve on the support desk;
without it, the person's role decides, with the same refusal. Keycloak and OpenFGA are now set up
under the name `kaas`: after pulling this change run `docker compose down -v` once so they are rebuilt.

Set `GITHUB_REPO` and `GITHUB_TOKEN` to post approved replies to a real repository; without them the
replies go to the fixture copy under `.mission/`.

## Built in earlier sessions

**A Slack AI agent with memory.** Tag the engineering analyst in a Slack thread. It searches GitHub,
Linear and the docs, replies citing each source, and remembers the thread. A citation to anything it
did not read in that run is removed before it reaches Slack.

```
make ask Q="is the export timeout a known bug?"
make slack                         # the analyst in your workspace, over Socket Mode
```

**A company brain with permissions.** Every issue, ticket and doc carries the groups that may read it,
and every search runs as the user who asked. The restricted text never reaches the model for a
contractor. Without `GITHUB_REPO` and `DOCS_DIR` it reads the `fixtures/kaas` tenant.

```
make ask Q="why does the CSV export time out?" AS=U_CONTRACTOR
```

## Layout

- `workforce/mission.py`: the manager, the support resolver, the analyst handoff and the
  maker-checker gate.
- `workforce/board.py`, `workforce/people.py`, `workforce/issues.py`: the board and its pages, the
  sign-in, and the GitHub issues each card is checked against.
- `web/`: the board's styles and font.
- `workforce/sources.py`, `workforce/analyst.py`, `workforce/slack_app.py`: the readers, the analyst
  and its citation check, and the Slack handler.
- `workforce/access.py`, `workforce/traces.py`, `compose.yaml`, `deploy/`: the deploy step, Keycloak
  and OpenFGA in front of every tool, and the Langfuse exporter.
- `tests/`: one acceptance test per built step, and `test_steps.py`, which fails when a built step
  has none.
