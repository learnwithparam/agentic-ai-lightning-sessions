# Agentic AI Lightning sessions

The code from each free Lightning session on agentic AI. Each session builds one working system for a
named company, checks it against a source of truth outside the model, and measures it with a test you
can run offline.

| Session | What you build | Code |
| --- | --- | --- |
| [Build a Mission Control for AI Agents](https://www.learnwithparam.com/labs/build-an-ai-mission-control-app) | AI employees work Acme's GitHub issues. A reply to a customer waits for a human. A card closes only when GitHub shows the reply. | [`mission-control/`](mission-control/) |
| Build a Multi-Agent System with MCP | A trip planner, a booker and a messenger rebook a cancelled flight, each through an MCP gateway that lists only its tools. The card closes only when the travel system holds a ticket that lands in time and both contacts were told. | [`multi-agent-mcp/`](multi-agent-mcp/) |
| Secure AI Agents with Guardrails | A screener shortlists payroll applicants, and one CV hides an instruction in white text. A guardrail withholds that CV from the model and holds the card for a recruiter; a request that screens by age stops before the model is called. | [`guardrails/`](guardrails/) |

Later sessions are listed in [`sessions.toml`](sessions.toml) and get a folder here once they are built.

```
make check                         # every built session's gate, offline: no keys, no spend
make -C mission-control demo FRESH=1
```

Each folder has its own README, `.env.example` and `make check`. Python with [uv](https://docs.astral.sh/uv/).
