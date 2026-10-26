# Software Factory teaching material

The run sheets, demo scripts and worksheets of the Software Factory Lightning sessions and cohort
modules, moved here from the factory repo when it became KaaS Factory (KaaS-team/KaaS-factory,
v5.1.0). They are kept as they stood at factory v5.0.1 and nothing here is gated.

- `sessions.json` maps each session to the factory release it teaches and its splitbill-demo
  checkpoint tag.
- `lightning-2.md` is the live run sheet; `demo/lightning-2.sh` builds its tmux layout. Run it from
  a factory checkout at tag v5.0.1, where `teach/` still exists.
- `worksheets/` is the static site at https://learnwithparam.github.io/agentic-ai-lightning-sessions/,
  published by `.github/workflows/worksheets.yml`.
- `research/` holds the notes on Claude memory and Anthropic pricing the factory was built from.
