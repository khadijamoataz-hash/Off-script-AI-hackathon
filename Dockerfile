FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml LICENSE README.md CAPABILITIES.md ARCHITECTURE.md ./
COPY orchestrator ./orchestrator
COPY agents ./agents
COPY policy ./policy
COPY ledger ./ledger
COPY outbox ./outbox
COPY llm ./llm
COPY ui ./ui
COPY scripts ./scripts
COPY DECISIONS.jsonl ./DECISIONS.jsonl

RUN pip install --no-cache-dir -e .

EXPOSE 8000
CMD ["uvicorn", "ui.app:app", "--host", "0.0.0.0", "--port", "8000"]
