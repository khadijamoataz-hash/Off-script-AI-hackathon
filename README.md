# Foundry

Open-source team of AI agents that takes a first-time founder from idea to a validated, sellable digital product—with the founder in control at every gate.

- **License**: Apache-2.0 (`LICENSE`)
- **Architecture**: See `ARCHITECTURE.md`
- **Honest Capability Status**: See `CAPABILITIES.md` (`REAL` vs `STUB`)

## Quick Commands

```bash
make setup       # Create .venv and install dependencies
make test        # Run unit, integration, and safety tests
make verify-log  # Verify SHA-256 hash chain and JSON Schema of DECISIONS.jsonl
make up          # Start via Docker Compose (or local uvicorn if Docker is not installed)
```

## Staged Build Roadmap
- **Stage 1 (Complete)**: Repo scaffold, Apache-2.0 license, SQLite Evidence Ledger, SHA-256 hash-chained Decision Log (`DECISIONS.jsonl` + `make verify-log`).
- **Stage 2**: Policy Engine ("must never" rules in code), Outbox & Signed Single-Use Approval Tokens, Red-Team Safety Suite.
- **Stage 3**: Ollama LLM Adapter (structured outputs + retry/repair) & 6-Phase State Machine Orchestrator.
- **Stage 4**: Phase Agents (Discover, Validate, Build sandbox, Commercial setup, Launch outbox, Operate).
- **Stage 5**: FastAPI + HTMX Founder Dashboard, CLI, and Live End-to-End Demo.
