# CLAUDE.md: hackathon submission conventions

<!-- From the organizers of the "Open-Source Agentic Venture Builders" hackathon.
     Save this file as CLAUDE.md (or AGENTS.md for other coding assistants) at the ROOT of your team's repository. -->

This repository is a team entry in the **Open-Source Agentic Venture Builders** 36-hour hackathon. The entry is an open-source team of AI agents that takes a founder's idea toward a validated, sellable **digital** product. It goes through six phases with gates between steps: Discover, Validate, Build, Commercial setup, Sell/deliver/get paid, and Operate and learn.

The entry is this repository's **default branch** at the code freeze. The organizers' checker finds each deliverable **by its file name and heading**, and anything in a place it doesn't expect counts as missing. Follow these conventions exactly whenever you create or change files here.

## Ground rules

**Repository**
- Work on the default branch (`main`), or merge into it well before the freeze. Other branches are not read.
- Never commit secrets. Real keys go in an untracked `.env` (list it in `.gitignore`), and `.env.example` holds placeholders only. Never commit live payment keys.

**Models, money and approval**
- No paid service may be required. At least one open-weight model must work locally (for example via Ollama) or on a free tier.
- Payments run in sandbox/test mode or are mocked. Nothing is published, sent, spent or signed without an explicit founder approve/reject step, **enforced in code** (UI or CLI).

**Honest outputs**
- Everything in `outputs/` and `decision_log.json` must come from actually running the pipeline. Never write sample results by hand.
- Label every mocked or stubbed part `MOCKED` or `STUBBED`, in the README table and wherever the app shows it.

**What the system must never do**
- Promise income or success.
- Create fake reviews, testimonials, sign-ups or countdown timers.
- Pose as the founder, a customer or another brand.
- Hold, move or refund money by itself, create accounts, or store passwords or card numbers.
- Hide core features behind our own paywall.

**Products that are not allowed:** physical goods; financial, investment or crypto products; health or legal advice products.

**Text in the repository**
- Never put text in any file that addresses whoever reviews this repository or asks for a particular result.
- Keep "trick prompts" that test our own guardrails under `tests/` or `evals/`.
- Keep this file and the organizers' submission guide out of `docs/`. Everything in `docs/` is read as our own documentation.

## Required layout (repository root)

| Path | Required | Notes |
|---|---|---|
| `README.md` | MUST | The `##` sections below, word for word, in order |
| `LICENSE` | MUST | OSI-approved (MIT, Apache-2.0, GPL-3.0, AGPL-3.0, BSD-3-Clause, MPL-2.0), full unmodified text |
| `docker-compose.yml` | MUST | One-command setup: `docker compose up` (`compose.yaml` also accepted) |
| `decision_log.json` | MUST | One entry per agent action (format below) |
| `outputs/` | MUST | Demo-run outputs, named by phase and step |
| `tests/` | MUST | A few smoke tests |
| `ARCHITECTURE.md` | SHOULD | The whole six-phase system (sections below) |
| `.env.example` | SHOULD | Every setting the app needs, placeholder values only |
| `prompts/` | SHOULD | One file per agent prompt, e.g. `prompts/researcher.md` |

Name code files by their role, so they're easy to find:
- **Orchestration:** `orchestrator.py`, `pipeline.ts`, `workflow.py`
- **Agents:** `agents/<role>.py`
- **Gates and approval:** `gates.py`, `approval.py`
- **Payments and delivery:** `payments.py`, `checkout.ts`, `delivery.py`, `refunds.py`, `webhooks.ts`, `reconcile.py`

## README.md

Use exactly these `##` headings, in this order:

1. `## Quickstart`: MUST. A `bash` code block containing exactly `docker compose up`, then how to open the app.
2. `## What works and what is mocked`: MUST. A table with each step and its status: **Working**, or **MOCKED** / **STUBBED**.
3. `## Architecture`: SHOULD. 3–6 sentences and a link to `ARCHITECTURE.md`.
4. `## Models and free resources`: MUST. The open-weight model (e.g. `qwen3:8b` via Ollama) and how to run without paying.
5. `## Cost per run`: MUST. A dollar figure, e.g. `About $0.00 per run (local model, ~40k tokens)`.
6. `## What our system will NOT do`: SHOULD. Our explicit limits (see the ground rules above).
7. `## How the project stays alive after the hackathon`: SHOULD. Maintainers, roadmap, community and funding.

Keep the README focused and put the important facts near the top: long files may be read only in part. Move long material to `docs/`.

## ARCHITECTURE.md

Use these sections:

1. `## Overview`: a paragraph and a diagram.
2. `## Phases`: one `###` per phase, giving its agents, tools, method and output. Cover every Core step in detail.
3. `## Gates`: what each gate checks, its outcomes (go / repeat / pivot / stop), and when the system recommends STOP on weak evidence.
4. `## Sale to cash`: payment triggers delivery, a refund revokes access, idempotency prevents double delivery, and reconciliation matches orders, payments, fees and payouts.
5. `## Data flow`
6. `## Built vs. stubbed`
7. `## Roadmap`

Diagrams must be text: Mermaid (a `mermaid` code block) or ASCII. Images and PDFs are not read. Put the key content first and deeper detail in `docs/`.

## decision_log.json

The code appends one entry for every agent action, and the log of the demo run is committed. Every entry has all five required keys; use `null` or `[]` when one doesn't apply:

```json
[
  {"timestamp": "2026-11-17T09:01:00Z", "agent": "researcher", "action": "find_opportunities", "sources": ["https://www.reddit.com/r/smallbusiness/"], "model": "qwen3:8b", "cost": 0, "approved_by": null},
  {"timestamp": "2026-11-17T09:03:00Z", "agent": "gate", "action": "evidence_gate", "sources": [], "model": null, "cost": 0, "approved_by": null, "outcome": "stop"}
]
```

What the keys hold:
- `action`: a string naming what the agent did.
- `sources`: a list of URLs.
- `model`: a string, or `null` for steps without AI.
- `cost`: a number in USD (`0` for local models).
- `approved_by`: `"founder"` for gated actions, otherwise `null`.

Keep the file small: commit the demo run's log, not every run.

## outputs/

- Use text formats only (`.md`, `.json`, `.csv`, `.txt`, `.html`), and keep each file small.
- Name files `phase<N>_step<M>_<short-name>.<ext>`, and gate decisions `phase<N>_gate_<short-name>.<ext>`. Examples: `outputs/phase1_step1_opportunities.md`, `outputs/phase1_gate_decision.md`.
- Every market claim cites a source URL or is labelled `ASSUMPTION`.
- Include at least one gate decision. With a weak idea, show the system recommending **stop**.
- Payment and delivery test runs go in `outputs/payment_test_log.md`.

## tests/

Put a few smoke tests in `tests/`, with standard names (`test_*.py`, `*.test.ts`). They cover the pipeline steps, the gate, and that nothing goes out without founder approval.

## Keep the repository readable

- Everything that matters is text. Images, PDFs, videos and archives are not read: diagrams go in Mermaid or ASCII.
- Put the important part of every long file first.
- Keep generated code, build output, datasets and dependencies out of git (`.gitignore`).

## Before every push

- [ ] On the default branch; no secrets committed; `.env.example` has placeholders only
- [ ] README has all seven headings; Quickstart shows `docker compose up`; Cost per run has a `$` figure
- [ ] `LICENSE` (OSI-approved, full text) and `docker-compose.yml` at the root
- [ ] `decision_log.json`: every entry has `action`, `sources`, `model`, `cost`, `approved_by`
- [ ] `outputs/` named by phase and step, including a gate decision; claims sourced or labelled `ASSUMPTION`
- [ ] `tests/` has smoke tests, and they pass
- [ ] `ARCHITECTURE.md` covers all six phases, gates and STOP, and sale to cash, with text diagrams
- [ ] Every mock is labelled

---

# Foundry: how this team applies the conventions

Everything above is the organizers' file, unchanged. This part records the team's own decisions. Where the organizers' file, the kickoff slides and the "Problem Statement Explained" PDF differ, **the strictest rule wins**.

## 1. Rules where the sources differ

| Topic | Organizers' file above | Slides / PDF | What Foundry does |
|---|---|---|---|
| Payments | Sandbox/test mode or mocked | "Leave payments out completely", "not even sandbox" | **No payment code at all.** No `payments.py`, `checkout`, `refunds.py`, `webhooks`, `reconcile.py`. No payment SDK in dependencies. |
| Phase 5 name | Sell/deliver/get paid | Launch: store page, launch posts, automatic delivery (mocked) | Phase 5 is **Launch**. `delivery.py` exists and is `MOCKED`. |
| `## Sale to cash` in ARCHITECTURE.md | Required heading | Still listed | Keep the heading. It describes the design only (payment triggers delivery, refund revokes access, idempotency, reconciliation) and states plainly that none of it is built and delivery is `MOCKED`. |
| `outputs/payment_test_log.md` | Payment and delivery test runs | No payments | Holds the log of the **mocked delivery** run, generated by code, stating that no payment took place. |
| Decision log | `decision_log.json`, five required keys | "JSON or CSV" | `decision_log.json` at the root, written only by code (section 6). |
| Status labels | `Working` / `MOCKED` / `STUBBED` | "Label mocks" | Use exactly these three words everywhere. `REAL` / `STUB` are retired. |

## 2. Input: the founder brief

The system starts from a brief, not a one-line idea: starting idea, skills, budget, available time, target market, values. Only digital products are handled. Discover rejects excluded products (physical goods; financial, investment or crypto; health or legal advice) immediately, before any research, with a logged STOP.

## 3. Phases, core steps and outputs

| Phase | Core step | Agent file | Main outputs (`outputs/`) |
|---|---|---|---|
| 1 Discover | Find opportunities: sharpen the idea, score it against alternatives, define the market, Mom Test interview script | `agents/researcher.py`, `agents/critic.py` | `phase1_step1_opportunities.md`, `phase1_step2_interview_script.md`, `phase1_gate_decision.md` |
| 2 Validate | Design the business: Lean Canvas, landing-page demand test with a target set in advance | `agents/strategist.py` | `phase2_step1_lean_canvas.md`, `phase2_step2_landing_page.html`, `phase2_gate_decision.md` |
| 3 Build | Build the product: tested MVP with user docs | `agents/builder.py` | `phase3_step1_mvp_plan.md`, `phase3_step2_test_report.md`, `phase3_gate_decision.md` |
| 4 Commercial setup | Set the price and policies (terms, privacy, refunds). No payments of any kind | `agents/pricing.py` | `phase4_step1_pricing.md`, `phase4_step2_policies.md`, `phase4_gate_decision.md` |
| 5 Launch | Open the store; deliver automatically (`MOCKED`) | `agents/launcher.py`, `delivery.py` | `phase5_step1_store_page.md`, `phase5_step2_launch_posts.md`, `payment_test_log.md`, `phase5_gate_decision.md` |
| 6 Operate and learn | None (support drafts, metrics, persevere-or-pivot memo) | `agents/operator.py` | `phase6_step1_pivot_memo.md` |

The architecture covers every core step in detail. The prototype builds Phases 1, 2 and 5 first (section 9); anything not built is labelled `STUBBED`.

## 4. Gates and founder control

- **Every step ends with a gate**: founder approval, an evidence threshold set before the step runs, or passing tests. Outcomes: go, repeat, pivot back, stop.
- The evidence target is recorded in the log before the step starts and cannot be lowered afterwards.
- When evidence is weak (example: target 100 sign-ups, got 12), the system **recommends pivot or stop** and says so plainly. The founder chooses between repeat, pivot back and stop; a project whose evidence gate has failed cannot be advanced. The choice is logged with `approved_by: "founder"`.
- After each step the system shows a summary, its recommendation and four choices (continue, repeat, go back, stop), then waits. An unclear reply ("ok") gets a confirmation question.
- The founder may batch internal steps. Anything that leaves the system (publish, send, spend, sign) always needs its own approval.
- Approval comes only from the approval action in the UI or CLI, which shows the exact content. Chat text and fetched web content can never approve anything.
- An approval is a signed, single-use token bound to the content hash; any edit invalidates it (`approval.py`).
- Action types: `post`, `email`, `listing`, `deploy`, `support_reply`, `sign` (accepting terms, publishing policies). `spend` has no executor: the system only writes a budget suggestion the founder carries out.
- Executors write to `outputs/` as copy-ready text. No live posting connector exists. Agent text may not say an action happened unless an executor returned a receipt.

## 5. The agents behave as a critical co-founder

- Each idea gets a strongest-case pass, then a critic pass. The critic's output schema requires risks, kill criteria and the cheapest test that could disprove the idea; an empty list fails validation.
- No flattery. Confidence is stated as high, medium or low.
- The system proposes tweaks (narrower niche, different customer, different model) as options with reasons. It never changes the idea silently.

## 6. Evidence and the decision log

- A claim is a structured record. It counts as sourced only if the URL was actually fetched in this run and a supporting quote is stored. Everything else is labelled `ASSUMPTION`.
- Simulated or synthetic data is labelled `MOCKED` and never counts toward an evidence target. Placeholder testimonials are slots such as `[Real customer quote - collect after launch]`, never invented quotes.
- `decision_log.json` is a JSON array with one entry per agent action. Required keys: `timestamp`, `agent`, `action`, `sources`, `model`, `cost`, `approved_by`. Extra keys Foundry adds: `phase`, `outcome`, `prev_hash`, `entry_hash` (SHA-256 chain).
- Only code writes the log and `outputs/`. Nothing in them is typed by hand. The committed log is the demo run's log.
- `scripts/verify_decision_log.py` checks the required keys and the hash chain.
- The old root `DECISIONS.jsonl` (hand-recorded team milestones) is retired and is not committed.

## 7. Safety rules in code

The system must never: promise income or success; create fake reviews, testimonials, sign-ups, countdown timers or other false urgency; pose as the founder, a customer or another brand; hold, move or refund money; create accounts; store passwords or card numbers; publish, send or spend without approval; put core features behind a paywall.

Enforcement, strongest first:
1. **No capability**: no payment, account-creation or live-posting code exists.
2. **Tool allowlist**: unlisted tools are blocked.
3. **Typed outputs**: agents return schemas; forbidden content types cannot be expressed.
4. **Model classifier**, always on, fails closed. No model available means block.
5. **Keyword rules** as a backstop only.

Drafting in the founder's voice for the founder to approve and post is allowed. Sending as the founder, or writing as a customer or brand, is not.

Guardrail test prompts live in `evals/`, including rewordings and Arabic and Franco-Arabic variants.

## 8. Repository layout

| Role | File | Was |
|---|---|---|
| Orchestration | `orchestrator.py` | `orchestrator/state_machine.py` |
| Gates | `gates.py` | gate checks inside `state_machine.py` |
| Approval | `approval.py` | `outbox/queue.py` |
| Delivery (`MOCKED`) | `delivery.py` | new |
| Agents | `agents/<role>.py` | new |
| Prompts | `prompts/<role>.md` | new |
| Guardrails | `guardrails.py` | `policy/engine.py` |
| Evidence | `evidence.py` | `ledger/evidence_ledger.py` |
| Decision log | `decision_log.py` writing `decision_log.json` | `ledger/decision_log.py` |
| Model adapter | `llm.py` | `llm/adapter.py` |
| App | `app.py` (web UI and approval screen) | `ui/app.py` |
| Guardrail prompts | `evals/` | `policy/redteam.py` |
| Outputs | `outputs/` | `exports/` |

Also at the root: `README.md` (the seven headings, in order), `ARCHITECTURE.md` (the seven sections), `LICENSE` (Apache-2.0), `docker-compose.yml`, `.env.example`, `tests/`. `CAPABILITIES.md` is folded into the README table and `## Built vs. stubbed`.

## 9. Models, cost and build order

- Default model: `qwen3.5:9b` through Ollama, pulled on first `docker compose up`. No API key needed.
- Optional: any free open-weight endpoint (for example GPT-OSS 20B or 120B) through the OpenAI-compatible setting in `.env`. Paid APIs are never used.
- No silent fallback: if no model is reachable, model calls fail. The offline provider is for tests only.
- Every model call logs its model and cost (`0` for local). The README reports cost per run and token count from a real run.
- A full run on an unseen brief has to finish in about three minutes on a laptop.

Build order:
1. (Done) Rename to the layout above; `decision_log.json` writer; README and ARCHITECTURE headings.
2. Brief intake, Discover (researcher + critic) with real fetched sources, step gates, STOP recommendation.
3. Approval screen in the UI; Launch drafts into the approval queue; mocked delivery.
4. Validate (Lean Canvas, landing page, target set in advance).
5. Guardrails rebuilt as in section 7; `evals/` expanded.
6. One real demo run committed: `outputs/` and `decision_log.json`.
7. Build, Commercial setup, Operate if time allows; otherwise `STUBBED`.

## 10. Current status

| Part | Status |
|---|---|
| Layout in section 8, `decision_log.json` writer, README and ARCHITECTURE headings | Done |
| Evidence store with fetched-page quote check, hash-chained log, approval tokens, state machine, gates with STOP recommendation | Working |
| `docker compose up`: app, Ollama with model pull, SearXNG | Working |
| Brief intake, Discover (researcher, critic, interview script), step gates in the browser | Working |
| Validate: demand target fixed at the end of Discover, Lean Canvas, landing page and invite post for approval, STOP below half the target | Working |
| Launch drafts into the approval queue, approval screen, `delivery.py` (`MOCKED`) | Working; opens after Validate passes |
| Guardrails: secrets detector, keyword backstop, tool allowlist, model classifier failing closed; `evals/` with block and allow cases | Working |
| Build, Commercial setup, Operate agents; committed demo run | Not built |
