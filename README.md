# AI workforce

The agentic AI system that the learnwithparam buildcamp, workshops and Live Labs build: AI employees in
Slack that work in company tools, with identity, permissions, durable tasks, evals and a cost per
tenant. Each session adds one step, listed in `steps.toml` with the promise its test proves.

## Step 1: a Slack AI agent with memory

Tag the engineering analyst in a Slack thread. It reads the thread, searches GitHub, Linear and the
docs, and replies in the thread citing each source. A follow-up in the same thread remembers the
earlier answer. A citation to anything it did not read in that run is removed before it reaches Slack.

```
make check                         # lint and every step's test, offline: no keys, no spend
cp .env.example .env               # then fill it in
make ask Q="is the double charge known?"
make slack                         # the analyst in your workspace, over Socket Mode
```

Without `GITHUB_REPO` and `DOCS_DIR` it reads the `fixtures/acme` tenant. Linear reads the fixtures
until a later step.

## Step 2: a company brain with permissions

Every issue, ticket and doc carries the groups that may read it, and every search runs as the user who
tagged the analyst. Ask the same question as an engineer and as a contractor and each is answered from
different sources; the restricted text never reaches the model for the contractor. A user in no group,
or a doc missing from `access.json`, matches nothing.

```
make ask Q="why are customers charged twice?" AS=U_CONTRACTOR
```

## Layout

- `workforce/sources.py`: the readers and the search.
- `workforce/analyst.py`: the agent, its tools and the citation check.
- `workforce/slack_app.py`: the Slack handler.
- `tests/`: one acceptance test per built step, and `test_steps.py`, which fails when a built step
  has none.
