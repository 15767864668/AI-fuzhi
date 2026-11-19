# Studio V2 — 10-module delivery plan

This branch is an incremental implementation branch. The current Gradio application
on `main` remains unchanged. **This document is a delivery specification, not a
claim that all modules are implemented.**

## Modules and acceptance criteria

1. **H3 API and cloud adapter** — official API and configurable remote adapters; connection test, capability discovery, timeouts, retries, redacted logs, resumable tasks; mocked integration tests and an optional real-key smoke test.
2. **Project management** — durable project index, autosave, rename, duplicate, delete with confirmation, backups, migration of existing `autosave.json`, and asset integrity checks. The first service foundation is `hongguo/project_catalog.py`.
3. **Character/scene/media library** — managed asset metadata, reference images, scene presets, thumbnails, content hashes, and safe deletion; no secrets in exported bundles.
4. **Visual storyboard** — editable shot cards, duration, dialogue, characters, references, prompt, generation status, preview, regenerate, reorder.
5. **AI script and shot breakdown** — configurable LLM adapter, schema-validated episodes/shots, human approval and editable results; no hard-coded paid provider.
6. **AI image generation** — provider adapter, reference-image workflows, image history, image-to-shot linking.
7. **Free canvas** — Vue 3 + Vue Flow, pan/zoom, drag/drop, typed nodes/edges, project persistence, undo/redo, safe workflow execution.
8. **Continuity / first-last frames** — frame extraction, shot continuity metadata, provider capability checks, clear fallback when API does not support frame conditioning.
9. **Visual timeline editing** — clip arrangement, trims, BGM, subtitles, transitions, preview, FFmpeg export.
10. **AI agent** — explicit user approval for tool actions, project-scoped context, bounded steps, cost caps, rollback and audit trail.

## Proposed architecture

- `frontend/`: Vue 3 + TypeScript; visual UI and canvas.
- `api/`: FastAPI; validated local HTTP endpoints, auth/session protection if network exposure is ever enabled.
- `hongguo/`: retain the existing generation, rendering and storage modules.
- `data/`: local SQLite metadata and project media folders; migrations and backups.
- `tests/`: unit, API, workflow and media smoke tests.

## Safety and compatibility gates

- Never write API keys to GitHub, logs, project bundles, or frontend persistence.
- Keep the existing Gradio entry point usable during migration.
- Preserve existing `.hgproj` imports and the six export presets.
- All destructive actions require confirmation; clone assets into the new project.
- Never silently invoke paid AI generation without explicit user action.
- Test offline with mocked APIs before optional live H3 generation.
- Merge into `main` only after all tests pass and UI workflows are verified.

## Delivery status

| Module | Status |
|---|---|
| H3 API / cloud | Existing baseline; V2 enhancements pending |
| Project management | Service foundation added; UI and tests pending |
| Character/scene/media library | Pending |
| Visual storyboard | Pending |
| AI script / breakdown | Pending |
| AI images | Pending |
| Free canvas | Pending |
| Continuity frames | Pending |
| Visual timeline | Pending |
| AI agent | Pending |
