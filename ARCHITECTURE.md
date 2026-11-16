# Foundry Architecture

Foundry is an open-source team of AI agents that takes a first-time founder from an idea to a validated, sellable digital product—with the founder in explicit control at every gate.

## Core Principles
1. **Founder Control**: Nothing is published, sent, spent, or signed without a recorded founder approval.
2. **Evidence First**: Every factual claim lives in the SQLite Evidence Ledger with a verifiable `http(s)` source URL; any claim without a valid source URL is automatically downgraded to `ASSUMPTION`.
3. **Safety in Code, Not Just Prompts**: Hard "must never" rules are enforced by the Policy Engine on every agent output and tool call. If uncertain, the engine fails closed.
4. **Open & Simple**: Apache-2.0 licensed, single-command setup (`make up` / `docker compose up`), open-weight local models via Ollama by default with zero paid services required.
5. **Honest Capabilities**: `CAPABILITIES.md` marks every subsystem `REAL` or `STUB` accurately at every stage.

## System Topology

```text
Founder (Web UI or CLI)
   |
Orchestrator (6-Phase Finite State Machine + Gates)
   |--- Phase Agents: Discover, Validate, Build, Commercial, Launch, Operate
   |--- Shared Services: Evidence Ledger (SQLite), Decision Log (Hash-chained JSONL), Project Store
   |
Policy Engine (Hard Rules + Classifier Pass, Fail-Closed)
   |---> Outbox ---> Approval Queue ---> Founder Approval (HMAC Single-Use Content-Hash Token)
                                                |
                                     (Verified Token Only) ---> Export / Copy Executors
   |
LLM Adapter ---> Ollama (default open-weight model) / Optional Fallback Provider
```

## Module Boundaries
- `ledger/`: SQLite Evidence Ledger (`evidence_ledger.py`) and SHA-256 hash-chained Decision Log (`decision_log.py`).
- `policy/`: Deterministic rule engine + optional model classifier pass blocking income guarantees, fake reviews/signups, impersonation, money handling, credential/card storage, and unapproved external actions.
- `outbox/`: Proposed external actions queue, founder approval/rejection workflow, HMAC-SHA256 content-bound single-use tokens, and copy-ready/file executors.
- `llm/`: Common structured-output interface over Ollama (`http://localhost:11434`) with JSON schema validation, repair retries, and an offline deterministic provider for CI/local testing when Ollama is unavailable.
- `orchestrator/`: Six-phase finite state machine (`discover` -> `validate` -> `build` -> `commercial` -> `launch` -> `operate`), gate evaluators (`advance`, `repeat`, `pivot_back`, `stop`), SQLite project store, and CLI (`foundry`).
- `agents/`: Phase-specific agents with bounded tools and strict Pydantic output schemas. Zero payment connectors exist anywhere in the codebase.
- `ui/`: FastAPI + HTMX dashboard surfacing phase timeline, evidence ledger, pending outbox approvals, live red-team runner, capability manifest, and decision log integrity status.
