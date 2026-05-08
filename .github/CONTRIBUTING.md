# Contributing

Thanks for your interest in contributing! This repository builds a compact, production-ready ComfyUI container optimized for RunPod.

## Adding a Changeset

When your PR includes user-facing changes, add a changeset so the release notes and version bump happen automatically:

```bash
npx changeset
```

You'll be prompted to pick a bump type (`patch`, `minor`, or `major`) and write a short description. This creates a markdown file in `.changeset/` — commit it with your PR.

- **patch** — bug fixes, dependency bumps
- **minor** — new features, non-breaking changes
- **major** — breaking changes (new image tag scheme, removed flags, etc.)

If your PR is purely internal (CI tweaks, docs, refactoring with no behaviour change), you can skip the changeset.

## Release Process

Releases are fully automated via [changesets](https://github.com/changesets/changesets):

1. **PR is merged to `main`** — the Changesets workflow runs. If pending changesets exist, it creates (or updates) a **"chore: version packages"** PR that bumps `package.json` and updates `CHANGELOG.md`.
2. **The "version packages" PR is merged** — the Changesets workflow runs again, detects the new version, and creates a **GitHub Release**.
3. **GitHub Release triggers `release.yml`** — Docker images are built and pushed to Docker Hub via `docker-bake.hcl`.

### Docker Image Tags

On each release (e.g. `2.0.0`):

| Tag | Description |
|---|---|
| `runpod/comfyui:2.0.0-cuda12.8` | Pinned release, CUDA 12.8 |
| `runpod/comfyui:2.0.0-cuda13.0` | Pinned release, CUDA 13.0 |
| `runpod/comfyui:cuda12.8` | Always latest CUDA 12.8 build |
| `runpod/comfyui:cuda13.0` | Always latest CUDA 13.0 build |
| `runpod/comfyui:latest` | Always latest CUDA 12.8 (default) |

### Emergency Manual Release

If you need to release without going through changesets, use the **Release** workflow's manual dispatch (`workflow_dispatch`) and provide a version string.

### Prerequisites (repository secrets)

- `DOCKERHUB_USERNAME`
- `DOCKERHUB_TOKEN` (Docker Hub access token)

## Opening PRs

- Keep changes focused and include a clear rationale.
- Update `docs/conventions.md` if you change behavior that developers rely on.

## License

- GPLv3. By contributing, you agree your changes are licensed under GPLv3.
