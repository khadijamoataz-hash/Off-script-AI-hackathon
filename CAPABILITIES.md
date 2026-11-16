# Foundry Capability Manifest (`REAL` vs `STUB`)

> Honesty rule: Every feature in Foundry is explicitly marked `REAL` or `STUB`. Stubs or unbuilt stages are never presented as live integrations.

| Subsystem / Feature | Status | Notes |
|---|---|---|
| **Evidence Ledger (`ledger/evidence_ledger.py`)** | `REAL` | SQLite store; auto-downgrades any claim missing a valid `http(s)` URL to `ASSUMPTION`; Markdown export & claim ID verification. |
| **Decision Log (`ledger/decision_log.py`)** | `REAL` | Append-only JSONL (`DECISIONS.jsonl`) with JSON Schema validation, sequential numbering, and SHA-256 hash chaining (`make verify-log`). |
| **Policy Engine (`policy/`)** | `STUB` | Scheduled for **Stage 2** (hard rules, classifier hook, fail-closed blocking, red-team suite). |
| **Outbox, Signed Approvals & File/Copy Executor (`outbox/`)** | `STUB` | Scheduled for **Stage 2** (proposed actions, single-use content-hash tokens, export executor). |
| **External Live Social/Email/Ad Publishing Connectors** | `STUB` | Intentionally export-only (`exports/` files + copy-ready text) in prototype; agents cannot publish directly and founder performs final external posting. |
| **Payment / Money Handling Connectors** | `NONE` | Intentionally excluded by safety architecture. Foundry agents never hold, charge, or move money. |
| **LLM Adapter (`llm/`)** | `STUB` | Scheduled for **Stage 3** (Ollama HTTP client + structured schema validation/retry + offline deterministic test provider). |
| **Phase Orchestrator & Gate State Machine (`orchestrator/`)** | `STUB` | Scheduled for **Stage 3** (6-phase FSM: advance, repeat, pivot_back, stop). |
| **Phase 1–5 Agents (`Discover`, `Validate`, `Build`, `Commercial`, `Launch`)** | `STUB` | Scheduled for **Stage 4**. |
| **Phase 6 Agent (`Operate`)** | `STUB` | Scheduled for **Stage 4** (prototype scope clearly labeled). |
| **Web Dashboard & CLI (`ui/`, `orchestrator/cli.py`)** | `STUB` | Scheduled for **Stage 5**. |
