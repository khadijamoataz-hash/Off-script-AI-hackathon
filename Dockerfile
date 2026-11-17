FROM python:3.12-slim

WORKDIR /app

# Dependencies first, so code changes do not download them again. Long timeout and retries
# keep the build working on a slow connection.
COPY pyproject.toml ./
RUN python -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml', 'rb'))['project']['dependencies']))" > /tmp/requirements.txt \
    && pip install --no-cache-dir --default-timeout=120 --retries 10 -r /tmp/requirements.txt

COPY . .

EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
