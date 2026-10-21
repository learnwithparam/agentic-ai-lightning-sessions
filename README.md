# Agentic AI Lightning sessions

The code from each free Lightning session on agentic AI. Each session builds one working system for a
named company, checks it against a source of truth outside the model, and measures it with a test you
can run offline.

| Session | What you build | Code |
| --- | --- | --- |
| [Build a Mission Control for AI Agents](https://www.learnwithparam.com/labs/build-an-ai-mission-control-app) | AI employees work Acme's GitHub issues. A reply to a customer waits for a human. A card closes only when GitHub shows the reply. | [`mission-control/`](mission-control/) |

Later sessions are listed in [`sessions.toml`](sessions.toml) and get a folder here once they are built.

```
make check                         # every built session's gate, offline: no keys, no spend
make -C mission-control demo FRESH=1
```

Each folder has its own README, `.env.example` and `make check`. Python with [uv](https://docs.astral.sh/uv/).
