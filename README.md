# Agentic AI Lightning sessions

The code from each free Lightning session on agentic AI. Each session builds one working system for a
named company, checks it against a source of truth outside the model, and measures it with a test you
can run offline.

| Session | What you build | Code |
| --- | --- | --- |
| [Build a Mission Control for AI Agents](https://www.learnwithparam.com/labs/build-an-ai-mission-control-app) | AI employees work KaaS's GitHub issues. A reply to a customer waits for a signed-in support approver. A card closes only when GitHub shows the reply. | [`mission-control/`](mission-control/) |
| Build a Multi-Agent System with MCP | A trip planner, a booker and a messenger rebook a cancelled flight, each through an MCP gateway that lists only its tools. The card closes only when the travel system holds a ticket that lands in time and both contacts were told. | [`multi-agent-mcp/`](multi-agent-mcp/) |
| Secure AI Agents with Guardrails | A screener shortlists payroll applicants, and one CV hides an instruction in white text. A guardrail withholds that CV from the model and holds the card for a recruiter; a request that screens by age stops before the model is called. | [`guardrails/`](guardrails/) |
| Cut AI Agent Costs with Token Economics | KaaS Insurance reads accident photos and repair quotes into claim files. Three levers cut the cost per claim filed, and a claim over its budget waits for the adjuster. | [`token-economics/`](token-economics/) |
| Build a Second Brain with Claude Code | Claude Code files a consultant's notes into quoted Markdown, and an agent answers client questions from them over MCP. A question no note holds waits for the consultant. | [`second-brain/`](second-brain/) |
| Build AI Agent Evals from Production Traces | KaaS Pharma's labelled production runs gate a model swap. A swap that misses a side effect is refused, and one that passes waits for the medical information lead. | [`evals-from-traces/`](evals-from-traces/) |
| Trace AI Agents in Production | KaaS Dairy's agent is killed mid shift and resumes from its Langfuse spans without booking a repair twice. A shift over its budget waits for the maintenance lead. | [`trace-in-production/`](trace-in-production/) |
| Build an AI Agent Governance Framework | KaaS Hotels' four AI employees work under one policy file. A write over a limit waits for the named signer, and every write has one decision behind it. | [`governance/`](governance/) |

Later sessions are listed in [`sessions.toml`](sessions.toml) and get a folder here once they are built.

[`factory/`](factory/) keeps the teaching material of the earlier Software Factory sessions, as an
archive with no gate.

```
make check                         # every built session's gate, offline: no keys, no spend
make -C mission-control demo FRESH=1
```

Each folder has its own README, `.env.example` and `make check`. Python with [uv](https://docs.astral.sh/uv/).
