# Studio V2 development API

This is the first usable backend integration milestone, not the full ten-module studio.

## Start

From the repository root:

```bash
python -m pip install -r requirements-studio-v2.txt
python -m uvicorn api.app:app --host 127.0.0.1 --port 8765
```

Open http://127.0.0.1:8765/docs for the interactive API explorer.

Available endpoints:

- GET `/api/health`
- GET `/api/projects`
- POST `/api/projects`
- GET `/api/projects/{id}`
- PATCH `/api/projects/{id}`
- POST `/api/projects/{id}/duplicate`
- POST `/api/projects/{id}/delete` with `{"confirm":true}`

Run offline tests with `python -m pytest tests/test_project_catalog.py tests/test_studio_v2_api.py`.

**Important:** The API is designed for local development only. It has no authentication,
so never bind it to `0.0.0.0` or publish the port. A future frontend must use
an authenticated session before any remote access is permitted. The existing
Gradio app remains available and unaffected.
