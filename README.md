# Foundry

An open-source team of AI agents that takes a founder's brief toward a validated, sellable digital product, and recommends stopping when the evidence is weak. The founder approves every step. Licensed Apache-2.0.

## Quickstart

```bash
docker compose up
```

Then open http://localhost:8000.

The first start downloads the open-weight model (`qwen3.5:9b`, about 7.6 GB) in the background. The page says the model is not ready until the download finishes. No API key or account is needed. Enter a founder brief on the home page; `/status` shows live checks and what is mocked.

Other commands:

```bash
python -m pytest -q                       # smoke tests
python scripts/verify_decision_log.py     # check decision_log.json keys and hash chain
python scripts/run_delivery_test.py       # MOCKED delivery test, writes outputs/payment_test_log.md
python -m evals.run                       # guardrail evals, rules only (no model needed)
python -m evals.run --model               # all guardrail evals through the local model
python -m evals.run --model --repeat 3    # the same, 3 times, to measure how stable the answers are
```

## What works and what is mocked

| Step or part | Status | Where |
|---|---|---|
| Founder brief intake; physical goods and other excluded product types are rejected immediately, before any research | **Working** | `agents/intake.py`, `app.py` |
| Web search (self-hosted SearXNG, no key) and page fetch | **Working** | `tools.py`, `searxng/` |
| Evidence store: a claim is sourced only if its page was fetched and contains the quote, otherwise `ASSUMPTION` | **Working** | `evidence.py` |
| Phase 1 Discover: sharpened idea, scored alternatives, critic review, Mom Test interview script | **Working** | `agents/researcher.py`, `agents/critic.py`, `workflow.py` |
| Step gates in the browser: continue, repeat, go back, stop; unclear replies are refused | **Working** | `app.py`, `workflow.py` |
| Phase 2 Validate: Lean Canvas (each box sourced or `ASSUMPTION`), landing page and invite post for approval, demand target fixed before the test, STOP below half the target | **Working** | `agents/strategist.py`, `workflow.py` |
| Phase 3 Build: build the product | **STUBBED** | not built |
| Phase 4 Commercial setup: two or three price options with comparable prices (sourced or `ASSUMPTION`), drafts of the terms, privacy and refund policies, each approved by the founder as a `sign` action; Launch opens only when all three are approved | **Working** | `agents/pricing.py`, `workflow.py` |
| Phase 5 Launch: store page and launch post drafts, queued for approval; opens only after Commercial setup passes | **Working**; Build is stubbed, so drafts use the Discover, Validate and Commercial setup results | `agents/launcher.py`, `workflow.py` |
| Approve or reject the exact text in the browser; approved text is exported as a copy-ready file | **Working** | `app.py`, `approval.py` |
| Phase 5 Launch: automatic delivery to buyers | **MOCKED** | `delivery.py`: simulated order, no payment |
| Phase 6 Operate and learn | **STUBBED** | not built |
| Decision log with hash chain | **Working** | `decision_log.py`, `decision_log.json` |
| Six-phase state machine and gates, including the STOP recommendation | **Working** | `orchestrator.py`, `gates.py` |
| Guardrails: secrets detector, keyword backstop, tool allowlist (no model needed) | **Working** | `guardrails.py` |
| Guardrails: model classifier on the founder brief and on every draft entering the approval queue; fails closed | **Working**: 51 of 51 eval cases in each of 3 repeated runs with `qwen3.5:9b` (34 attacks blocked, 17 normal requests, briefs and policy drafts allowed, including Arabic) | `guardrails.py`, `llm.py`, `evals/results.md` |
| Local model adapter (Ollama) | **Working** | `llm.py` |
| Posting to social, email or store sites | **STUBBED** by design | approved text is written to `outputs/` for the founder to post |
| Payments | None | no payment code exists, not even sandbox |

Known limits: the keyword backstop is English only; rewordings, Arabic and Arabic in Latin letters depend on the model classifier, which is a small local model and can miss a cleverly worded request (measured results are in `evals/results.md`). What it cannot do is cause a payment, a post or an account: no code for those exists. Internal documents (the opportunities report, the interview script) get the keyword check only. Because Phase 3 is stubbed, a project that passes Validate stays recorded at Build while Commercial setup and Launch drafting run. The policy drafts are not legal advice and say so. Only demand the founder records (real sign-ups, interviews or pre-orders) counts toward the Validate target; fetched sources do not.

## Architecture

A state machine moves a project through six phases: Discover, Validate, Build, Commercial setup, Launch, Operate and learn. Every step ends with a gate: founder approval, an evidence target set in advance, or passing tests. Agents can only propose actions; anything that would be published, sent, spent or signed waits in an approval queue until the founder approves the exact content. Every claim is stored with a fetched source or labelled `ASSUMPTION`, and every agent action is written to a hash-chained `decision_log.json`. Agent outputs are typed, so content such as testimonials, prices or countdowns has no field to be written into. A secrets detector, keyword rules and a model classifier that fails closed check the founder's brief before any agent runs and every draft before it enters the approval queue. Full design: [ARCHITECTURE.md](ARCHITECTURE.md).

## Models and free resources

- Model: `qwen3.5:9b` (Apache-2.0), an open-weight model, run locally through Ollama. Its separate reasoning mode is off by default for speed; set `FOUNDRY_THINK=1` to turn it on. `qwen2.5:7b` also works (`FOUNDRY_MODEL=qwen2.5:7b`): it was as fast in our measurements but missed one more guardrail test. `docker compose up` starts Ollama and pulls the model.
- No paid service, API key or account is required. No paid API is ever called.
- To use a different open-weight model, set `FOUNDRY_MODEL` (see `.env.example`).
- If the model is not available, model calls fail with an error. There is no mock fallback outside the tests.
- Web search uses a SearXNG container started by the same command: no key, no account.
- Speed: inside Docker on a Mac the model runs on CPU (several minutes per step). To use a host Ollama with GPU access, set `FOUNDRY_OLLAMA_URL=http://host.docker.internal:11434` in `.env`.

## Cost per run

About $0.00 per run (local open-weight model `qwen3.5:9b`, no paid API).

Measured on the committed demo run (`outputs/`, `decision_log.json`): 10 agent model calls, 22,808 tokens (15,046 prompt, 7,762 completion), on an Apple M3 laptop. That run went through Discover three times (the founder chose repeat twice) and then Validate, so a single pass uses fewer. The guardrail checks make additional model calls that are not included in this count. Every entry in `decision_log.json` carries a `model` and a `cost` field.

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

- **Maintainers:** the Foundry team maintains the repository under Apache-2.0. Every pull request needs a passing test run (GitHub Actions, no model or paid service needed) and a review from a maintainer; changes to guardrails or approval code also need a new or updated eval case.
- **Cost to keep running:** about $0.00 per run. The model runs locally through Ollama and search runs in a local SearXNG container, so there are no servers, API keys or subscriptions to pay for.
- **How to contribute:** add an agent (`agents/` with its prompt in `prompts/` and tests), add a guardrail eval case (`evals/`), or write a playbook for a product type. See [CONTRIBUTING.md](CONTRIBUTING.md) and the [code of conduct](CODE_OF_CONDUCT.md).
- **Roadmap:** the `STUBBED` phases (Build, Operate) first, then a stronger guardrail model, wider evals and community playbooks. See the roadmap in [ARCHITECTURE.md](ARCHITECTURE.md).
- **Funding (`ASSUMPTION`, not yet validated):** a hosted version for founders who cannot run a model; licences and support for incubators and university venture programmes; GitHub Sponsors or Open Collective for maintenance.
- **Never paywalled:** the core features, the guardrails and the founder approval steps stay free and open in every version.
