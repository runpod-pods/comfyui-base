# ComfyUI Slim – Developer Conventions

This document outlines how to work in this repository from a developer point of view: build targets, runtime behavior, environment, dependency management, customization points, quality gates, and troubleshooting.

## Stack Overview

- **Base OS**: Ubuntu 24.04
- **GPU stack**:
  - Regular image: CUDA 12.8, PyTorch (pinned version via docker-bake.hcl, cu128 wheels)
  - RTX 5090 image: CUDA 13.0, PyTorch (pinned version, cu130 wheels)
- **Python**: 3.12 (set as system default inside the image)
- **Package manager**: pip + pip-tools (lock file generated at build time with `pip-compile --generate-hashes`)
- **Tools bundled**: FileBrowser (port 8080), JupyterLab (port 8888), OpenSSH server (port 22), FFmpeg (NVENC), common CLI tools
- **Primary app**: ComfyUI, with pre-installed custom nodes

## Repository Layout

- `Dockerfile` – Single Dockerfile for all variants (CUDA version controlled via build args)
- `start.sh` – Runtime bootstrap (shared by all variants)
- `docker-bake.hcl` – Buildx bake targets (`regular`, `dev`, `rtx5090`) and all version pins (single source of truth)
- `scripts/fetch-hashes.sh` – Fetches latest custom node commit hashes from GitHub
- `README.md` – User-facing overview
- `docs/context.md` – This document

At runtime, the container uses:

- `/workspace/runpod-slim/ComfyUI` – ComfyUI checkout and virtual environment
- `/workspace/runpod-slim/comfyui_args.txt` – Optional line-delimited ComfyUI args
- `/workspace/runpod-slim/filebrowser.db` – FileBrowser DB

## Build Targets

Use Docker Buildx Bake with the provided HCL file.

- `regular` (default production):
  - CUDA 12.8, PyTorch cu128
  - Tag: `runpod/comfyui:${TAG}` (defaults to `slim`)
  - Platform: `linux/amd64`
- `dev` (local testing):
  - Same as regular, output: local docker image (not pushed)
  - Tag: `runpod/comfyui:dev`
- `rtx5090` (Blackwell / 5090):
  - CUDA 13.0, PyTorch cu130
  - Tag: `runpod/comfyui:${TAG}-5090`

Example commands:

```bash
# Build default regular target
docker buildx bake -f docker-bake.hcl regular

# Build dev image locally
docker buildx bake -f docker-bake.hcl dev

# Build 5090 variant
docker buildx bake -f docker-bake.hcl rtx5090
```

Build args and env:

- `TAG` variable in `docker-bake.hcl` controls the tag suffix (default `slim`).
- Build uses BuildKit inline cache.

## Runtime Behavior

Startup is handled by `start.sh` (shared by all variants):

- Initializes SSH server. If `PUBLIC_KEY` is set, it is added to `~/.ssh/authorized_keys`; otherwise a random root password is generated and printed to logs.
- Exports selected env vars broadly to `/etc/environment`, PAM, and `~/.ssh/environment` for non-interactive shells.
- Initializes and starts FileBrowser on port 8080 (root `/workspace`). Default admin user is created on first run.
- Starts JupyterLab on port 8888, root at `/workspace`. Token set via `JUPYTER_PASSWORD` if provided.
- Ensures `comfyui_args.txt` exists.
- On first boot: copies baked ComfyUI and custom nodes from `/opt/comfyui-baked` to `/workspace/runpod-slim/ComfyUI/`, then creates a Python 3.12 venv with `--system-site-packages`.
- On subsequent boots: activates existing venv (no network calls).
- Starts ComfyUI **in the foreground** via `exec` (becomes PID 1) with fixed args `--listen 0.0.0.0 --port 8188` plus any custom args from `comfyui_args.txt`. Logs go directly to container stdout.

## Ports

- 8188 – ComfyUI
- 8080 – FileBrowser
- 8888 – JupyterLab
- 22 – SSH

Expose settings are declared in Dockerfiles.

## Environment Variables

Recognized at runtime by the start scripts:

- `PUBLIC_KEY` – If provided, enables key-based SSH for root; otherwise a random password is generated and printed.
- `JUPYTER_PASSWORD` – If set, used as the JupyterLab token (no browser; root at `/workspace`).
- GPU/CUDA-related environment variables are propagated (`CUDA*`, `LD_LIBRARY_PATH`, `PYTHONPATH`, and `RUNPOD_*` vars if present in the environment).

## Dependency Management

- Python 3.12 is the default interpreter in the image.
- Venv location:
  - CUDA 12.8 image: `/workspace/runpod-slim/ComfyUI/.venv-cu128`
  - CUDA 13.0 image: `/workspace/runpod-slim/ComfyUI/.venv-cu130`
  - The image writes its `TORCH_INDEX_SUFFIX` to
    `/opt/comfyui-torch-index-suffix`; `start.sh` uses it to select the
    matching venv. A workspace volume can therefore move between CUDA image
    variants without one variant's PyTorch wheels shadowing another's.
- All dependencies are pre-installed at image build time. No pip installs occur at runtime.
- **Version pins live in `docker-bake.hcl`** (single source of truth, not in the Dockerfiles). Dockerfiles declare `ARG` names but the default values are set in the bake file:
  - `COMFYUI_VERSION` — ComfyUI release tag
  - `MANAGER_SHA`, `KJNODES_SHA`, `CIVICOMFY_SHA`, `RUNPODDIRECT_SHA` — custom node commit hashes
  - `TORCH_VERSION`, `TORCHVISION_VERSION`, `TORCHAUDIO_VERSION` — PyTorch stack versions (regular image)
  - `TORCH_VERSION_5090`, `TORCHVISION_VERSION_5090`, `TORCHAUDIO_VERSION_5090` — PyTorch stack versions (5090 image, can diverge)
  - `CUDA_VERSION_DASH` — CUDA toolkit apt package suffix (e.g., `12-8`, `13-0`)
  - `TORCH_INDEX_SUFFIX` — PyTorch wheel index (e.g., `cu128`, `cu130`)
  - `FILEBROWSER_VERSION` + `FILEBROWSER_SHA256` — FileBrowser binary with checksum
- To update a version: edit the corresponding `variable` block in `docker-bake.hcl`.
- CI or ad-hoc builds can override any variable via environment variables:
  ```bash
  COMFYUI_VERSION=v0.15.0 docker buildx bake regular
  ```
- `scripts/fetch-hashes.sh` queries the GitHub API for the latest commit hash of each custom node repo and prints HCL-formatted variable blocks ready to copy-paste into `docker-bake.hcl`. Set `GITHUB_TOKEN` env var for authenticated requests (higher API rate limit).
- Source code is downloaded as zip archives from GitHub (no git clone in build or runtime).
- A lock file with SHA256 hashes is generated inside the builder stage using `pip-compile --generate-hashes`.
- PyTorch wheel index is controlled by `TORCH_INDEX_SUFFIX` build arg (`cu128` for regular, `cu130` for 5090).
- The runtime image writes `/opt/comfyui-runtime-constraints.txt` from the same PyTorch pins and `start.sh` exports it as `PIP_CONSTRAINT`, so legacy venv migration and ComfyUI-Manager dependency installs cannot silently replace the CUDA-matched torch stack.
- At runtime, baked ComfyUI is copied from `/opt/comfyui-baked` to `/workspace/runpod-slim/ComfyUI/` on first boot. On later boots, a bundle manifest detects image upgrades and refreshes ComfyUI core plus the four image-managed custom nodes. Models, inputs, outputs, user settings, virtual environments, and user-installed custom nodes are preserved. When a volume made by another CUDA image is attached, its venv remains untouched and a new matching venv is created; requirements declared by user custom nodes are reinstalled into the new venv.

Preinstalled custom nodes:

- `ComfyUI-Manager` (ltdrdata)
- `ComfyUI-KJNodes` (kijai)
- `Civicomfy` (MoonGoblinDev)
- `ComfyUI-RunpodDirect` (MadiatorLabs)

## Customization Points

- `comfyui_args.txt` – Add one CLI arg per line; comments starting with `#` are ignored. These are appended after fixed args.
- Add/remove custom nodes by adding/removing download blocks and ARGs in the Dockerfile.
- Additional system packages: modify the Dockerfile `apt-get install` lines.
- Users can install additional custom nodes at runtime via ComfyUI-Manager (user's responsibility, not baked).

## Dev Conventions

- Keep images lean. All Python dependencies are baked at build time via lock file.
- To update a dependency: bump the relevant variable in `docker-bake.hcl`, push, trigger build.
- Source archives are used instead of `git clone` — no git dependency in builds.
- Avoid changing ports; they are referenced by external templates (RunPod/UI tooling).
- Use Python 3.12. Do not downgrade in scripts.
- When adding new env vars needed by downstream processes, ensure they are exported in `export_env_vars()` the same way as others.
- Shell scripting: keep `set -e` at top; prefer explicit guards; write idempotent steps safe to re-run.
- Runtime `start.sh` must NEVER call pip, git clone, or execute arbitrary install scripts. All dependencies are baked in the image.

## Local Development Tips

- Use the `dev` target to build a locally loadable image without pushing:
  ```bash
  docker buildx bake -f docker-bake.hcl dev
  docker run --rm -p 8188:8188 -p 8080:8080 -p 8888:8888 -p 2222:22 \
    -e PUBLIC_KEY="$(cat ~/.ssh/id_rsa.pub)" \
    -e JUPYTER_PASSWORD=yourtoken \
    -v "$PWD/workspace":/workspace \
    runpod/comfyui:dev
  ```
- Mount a host `workspace` to persist ComfyUI, args, and FileBrowser DB.

## Troubleshooting

- ComfyUI not reachable on 8188:
  - Check container logs (ComfyUI runs in foreground, logs go to stdout).
  - Ensure `comfyui_args.txt` doesn't contain invalid flags (comments with `#` are okay).
- JupyterLab auth:
  - If `JUPYTER_PASSWORD` is unset, Jupyter may allow tokenless or default behavior. Set it explicitly if needed.
- SSH access:
  - If no `PUBLIC_KEY` is provided, a random root password is generated and printed to stdout. Check container logs.
  - Ensure port 22 is mapped from the host, e.g., `-p 2222:22`.
- GPU/torch issues on 5090 image:
  - Verify you're running the `-5090` tag.
  - 5090 builds use CUDA 13.0 (`cu130` wheels); confirm host driver supports CUDA 13.0 (driver 575+).

## Release & Tagging

- Default tag base is `slim` via `TAG` in `docker-bake.hcl`.
- For 5090 builds, the pushed tag is `${TAG}-5090`.
- Keep `README.md` ports and features in sync when changing defaults.

## License

- GPLv3 as per `LICENSE`.
