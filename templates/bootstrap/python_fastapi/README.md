# {{REPO_SLUG}}

{{DESCRIPTION}}

## Overview

A Python FastAPI project.

## Setup

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Visit `http://localhost:8000/healthz` to verify the server is running.

## API

| Method | Path | Description |
|---|---|---|
| GET | `/healthz` | Health check |

## Testing

```bash
pytest -q --tb=short
```

## Structure

```
app/
  main.py       — FastAPI app and routes
tests/
  test_health.py — health check test
requirements.txt
```
