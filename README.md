# Foundry

An open-source team of AI agents that takes a founder's brief toward a validated, sellable digital product, and recommends stopping when the evidence is weak. The founder approves every step. Licensed Apache-2.0.

## Quickstart

```bash
docker compose up
```

Then open http://localhost:8000.

The first start downloads the open-weight model (`qwen2.5:7b`, about 5 GB) in the background. The page shows "model still downloading" until it is ready. No API key or account is needed.

Other commands:

```bash
python -m pytest -q                       # smoke tests
python scripts/verify_decision_log.py     # check decision_log.json keys and hash chain
python -m evals.run                       # guardrail evals, pass/fail table
```

## What works and what is mocked

| Step or part | Status | Where |
|---|---|---|
| Evidence store (every claim is sourced or labelled `ASSUMPTION`) | **Working** | `evidence.py` |
| Decision log with hash chain | **Working** | `decision_log.py`, `decision_log.json` |
| Approval queue: signed, single-use, content-bound approvals | **Working** | `approval.py` |
| Six-phase state machine and gates, including the STOP recommendation | **Working** | `orchestrator.py`, `gates.py` |
| Guardrails: keyword rules and tool allowlist | **Working** | `guardrails.py` |
| Guardrails: model classifier on every output | **STUBBED** | built in `llm.py`, not switched on by default |
| Local model adapter (Ollama) | **Working** | `llm.py` |
| Status page | **Working** | `app.py` |
| Phase 1 Discover: find opportunities | **STUBBED** | `agents/` |
| Phase 2 Validate: design the business | **STUBBED** | `agents/` |
| Phase 3 Build: build the product | **STUBBED** | `agents/` |
| Phase 4 Commercial setup: price and policies | **STUBBED** | `agents/` |
| Phase 5 Launch: store page and launch posts | **STUBBED** | `agents/` |
| Phase 5 Launch: automatic delivery to buyers | **STUBBED** (will be **MOCKED**) | not built |
| Phase 6 Operate and learn | **STUBBED** | `agents/` |
| Approving actions in the browser | **STUBBED** | not built; approval works through the Python API |
| Posting to social, email or store sites | **STUBBED** by design | approved text is written to `outputs/` for the founder to post |
| Payments | None | no payment code exists, not even sandbox |

Known limit: the keyword guardrails miss rewordings and non-English prompts until the model classifier is on by default.

## Architecture

A state machine moves a project through six phases: Discover, Validate, Build, Commercial setup, Launch, Operate and learn. Every step ends with a gate: founder approval, an evidence target set in advance, or passing tests. Agents can only propose actions; anything that would be published, sent, spent or signed waits in an approval queue until the founder approves the exact content. Every claim is stored with a fetched source or labelled `ASSUMPTION`, and every agent action is written to a hash-chained `decision_log.json`. Guardrails run in code on every agent output and tool call. Full design: [ARCHITECTURE.md](ARCHITECTURE.md).

## Models and free resources

- Model: `qwen2.5:7b`, an open-weight model, run locally through Ollama. `docker compose up` starts Ollama and pulls the model.
- No paid service, API key or account is required. No paid API is ever called.
- To use a different open-weight model, set `FOUNDRY_MODEL` (see `.env.example`).
- If the model is not available, model calls fail with an error. There is no mock fallback outside the tests.

## Cost per run

About $0.00 per run (local open-weight model, no paid API). Every entry in `decision_log.json` carries a `model` and a `cost` field. A measured token count will be reported here once the pipeline runs end to end.

## What our system will NOT do

- Promise income or success.
- Create fake reviews, testimonials, sign-ups, countdown timers or other false urgency.
- Pose as the founder, a customer or another brand.
- Hold, move or refund money, or take payments of any kind.
- Create accounts, or store passwords or card numbers.
- Publish, send, spend or sign anything without the founder's approval of the exact content.
- Hide core features behind a paywall.
- Work on excluded products: physical goods; financial, investment or crypto products; health or legal advice products.
- Present a mock as a working feature. Mocked and stubbed parts are labelled here and in the app.

## How the project stays alive after the hackathon

- **Maintainers:** the Foundry team maintains the repository under Apache-2.0 and reviews contributions.
- **Roadmap:** phase agents for all six phases, the approval screen, guardrails with the model classifier on by default, then community playbooks per product type. See the roadmap in [ARCHITECTURE.md](ARCHITECTURE.md).
- **Community:** contributed agents, prompts (`prompts/`) and guardrail evals (`evals/`). The guardrail evals are public so anyone can check the safety rules.
- **Funding (`ASSUMPTION`, not yet validated):** the core stays free and self-hosted. Possible income is a hosted version for founders who do not want to run a model, and licences and support for incubators and university venture programmes. Safety rules and core features are never paywalled.
