from fastapi import FastAPI

app = FastAPI(title="{{REPO_SLUG}}")


@app.get("/healthz")
def health():
    return {"status": "ok"}
