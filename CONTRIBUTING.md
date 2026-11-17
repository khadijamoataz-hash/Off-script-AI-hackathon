# Contributing to Foundry

Thank you for helping. Foundry is a team of AI agents that takes a founder's brief toward a validated digital product, and recommends stopping when the evidence is weak. Contributions must keep it honest and safe: read the rules at the end before you open a pull request.

By taking part you agree to follow the [code of conduct](CODE_OF_CONDUCT.md).

## Setup

You need Python 3.11 or newer. Docker is only needed to run the full app with the local model and search.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

To run the whole app (model, search and web UI):

```bash
docker compose up
```

Then open http://localhost:8000. Copy `.env.example` to `.env` to change settings; never put real keys in a committed file.

## Running the tests and evals

The tests use a test-only offline provider, so no model is needed:

```bash
FOUNDRY_LLM_PROVIDER=offline python -m pytest -q
python scripts/verify_decision_log.py     # required keys and hash chain of decision_log.json
python -m evals.run                       # guardrail evals, rules only
```

With Ollama and the model available, run every guardrail eval through the model, three times in a row to see how stable the answers are:

```bash
python -m evals.run --model --repeat 3
```

GitHub Actions runs the tests and the decision log check on every push and pull request.

## Adding an agent

1. Write the agent in `agents/<role>.py`. An agent is a function that takes typed input, calls the model once through `run_agent` and returns a typed result (a Pydantic model). Agents never write files, call outside tools or log; `workflow.py` does that around them.
2. Write its prompt in `prompts/<role>.md`. Say what it must produce, what counts as a sourced claim, and the hard rules (no income promises, no testimonials, no urgency, no impersonation).
3. Make the output schema do the enforcing: required fields such as risks or kill criteria with a minimum length, validators that reject hashtags or emoji, and no field where forbidden content (reviews, countdowns, prices on a page that has no product yet) could go.
4. Add the step to `workflow.py`. It records claims in the evidence store, writes its output to `outputs/` as `phase<N>_step<M>_<name>.md`, logs one entry per action, and ends at a gate. Anything that leaves the system (`post`, `email`, `listing`, `deploy`, `support_reply`, `sign`) goes to the approval queue.
5. Add tests in `tests/` with scripted model replies (see `tests/test_workflow_discover.py`): the happy path, the gate refusing to continue, and nothing exported before approval.
6. Update the table in `README.md` and the phase in `ARCHITECTURE.md`, with the status `Working`, `MOCKED` or `STUBBED`.

## Adding a guardrail eval case

Cases live in `evals/redteam.py`:

- `_block(...)` for text that must be refused, with the expected category.
- `_allow(...)` for ordinary work that must not be refused. False blocks matter too: add one when a normal brief or draft is wrongly stopped.
- `needs_model=True` for rewordings and other languages that the keyword rules cannot catch.
- `surface="agent_output"` for drafts, `surface="tool_call"` with `tool_name=` for tool calls.

Run `python -m evals.run` (and `--model --repeat 3` if you can), and commit the updated `evals/results.md` only when it was produced by that run.

Trick prompts that test the guardrails belong only in `tests/` or `evals/`.

## Playbooks

A playbook is guidance for one product type (for example templates, short courses or small software tools): typical customers, where to find them, honest price ranges with sources, and common risks. Open an issue with the "New agent or playbook" template first so the format can be agreed.

## Rules

A pull request that breaks one of these is not merged:

- **No payment code.** No checkout, charges, refunds, payouts or payment SDKs, not even sandbox. The founder sells through their own provider.
- **No fake social proof.** No invented reviews, testimonials, ratings, sign-up counts or countdown timers, not even as samples. Use a placeholder such as `[Real customer quote - collect after launch]`.
- **No acting for the founder.** Nothing is published, sent, spent or signed without the founder approving the exact content in the UI. No account creation, no stored passwords or card numbers, no posting as the founder, a customer or another brand.
- **No hand-written results.** `decision_log.json` and everything in `outputs/` are written only by the code during a real run. Never edit them by hand.
- **Label every mock.** Anything simulated is labelled `MOCKED`, anything not built is `STUBBED`, in the README table and wherever the app shows it.
- **Evidence or `ASSUMPTION`.** A market claim is sourced only if its page was fetched in the run and contains the quote; everything else is labelled `ASSUMPTION`.
- **Free and open core.** Core features, guardrails and approval steps are never put behind a paywall.
- **No secrets in git.** Real keys go in an untracked `.env`; `.env.example` holds placeholders only.
