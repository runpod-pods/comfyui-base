# Changelog

All notable changes to this project will be documented in this file.

## 1.3.0

### Breaking Changes

- **Venv path renamed** from `.venv` to `.venv-cu128`. Existing users on the legacy (non-Blackwell) template will see a one-time re-setup on first boot after upgrading. Blackwell and newer users are unaffected.
- **Ubuntu 22.04 → 24.04** for both images. Python 3.12 is now provided by the base OS (deadsnakes PPA no longer needed).
- **CUDA upgraded**: regular image now uses CUDA 12.8 (cu128 wheels), CUDA 13.0 image uses cu130 wheels. CUDA 12.4 is no longer supported.
- **No runtime dependency installs**: all Python dependencies are baked into the image at build time. The start script no longer runs `pip install` or executes `install.py`/`setup.py` on boot. Custom nodes installed at runtime via ComfyUI-Manager are the user's responsibility; their dependencies persist in the venv across reboots.
- **Dockerfile.5090 and start.5090.sh removed**: a single `Dockerfile` and `start.sh` now serve all variants, controlled by build args in `docker-bake.hcl`.
- **ComfyUI runs in foreground**: `exec python main.py` replaces the old `nohup` + `tail -f` pattern. Logs go directly to container stdout instead of `/workspace/runpod-slim/comfyui.log`.
- **Docker image tag scheme changed**: tags now use CUDA version instead of GPU model name. See new tag scheme below.

### New Docker Image Tag Scheme

Tags now clearly identify the CUDA version. The old `5090`-suffixed tags are deprecated.

On each release (e.g. `2.0.0`):

| Tag | Description |
|---|---|
| `runpod/comfyui:2.0.0-cuda12.8` | Pinned release, CUDA 12.8 |
| `runpod/comfyui:2.0.0-cuda13.0` | Pinned release, CUDA 13.0 |
| `runpod/comfyui:cuda12.8` | Always latest CUDA 12.8 build |
| `runpod/comfyui:cuda13.0` | Always latest CUDA 13.0 build |
| `runpod/comfyui:latest` | Always latest CUDA 12.8 (default) |

**Deprecated tags** (no longer produced):
- `runpod/comfyui:*-5090`
- `runpod/comfyui:latest-5090`

### Added

- Centralized version pinning in `docker-bake.hcl` (single source of truth for ComfyUI, custom node SHAs, PyTorch, FileBrowser).
- Hash-verified dependency lock file generated at build time via `pip-compile --generate-hashes`.
- `scripts/fetch-hashes.sh` to query GitHub API for latest custom node commit SHAs.
- `scripts/prebake-manager-cache.py` to pre-populate ComfyUI-Manager cache at build time, reducing cold start time.
- ComfyUI-RunpodDirect added as a pre-installed custom node.
- Git init with tagged commits and upstream remotes at build time so ComfyUI-Manager can detect versions.
- FileBrowser pinned to a specific version with SHA256 checksum verification.
- PyTorch 2.10.0 + torchvision 0.25.0 + torchaudio 2.10.0 for both images.
- Separate PyTorch version pins for regular and CUDA 13.0 images so versions can diverge independently.

### Removed

- `Dockerfile.5090` (unified into `Dockerfile` with build args)
- `start.5090.sh` (unified into `start.sh`)
- Runtime `git clone` and `pip install` loops from start script
- `golang` and `make` from runtime image (no longer needed without FileBrowser build-from-source)
- deadsnakes PPA and ffmpeg-nvenc PPA dependencies
- `5090`-suffixed Docker image tags (replaced by CUDA-versioned tags)
