# AI Dev Orchestrator

AI-assisted software development workflow system — Phase 1 (Foundation).

## What it does (Phase 1)

- Receives Jira webhook events
- Stores events in PostgreSQL
- Dispatches stub workflows via a Redis-backed queue
- Notifies the user via Telegram

## Project Structure

```
ai-dev-orchestrator/
├── app/
│   ├── __init__.py
│   └── main.py        # FastAPI app, /healthz endpoint
├── requirements.txt
└── README.md
```

## Local Setup & Run

### Prerequisites

- Python 3.11+

### 1. Create and activate a virtual environment

```bash
python -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Start the server

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

### 4. Test the health endpoint

```bash
curl http://localhost:8000/healthz
```

Expected response:

```json
{"status": "ok"}
```

You should also see a log line in the terminal:

```
[INFO] orchestrator: Health check called
```
