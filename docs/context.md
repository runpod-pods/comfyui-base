# ComfyUI Slim – Developer Conventions

This document outlines how to work in this repository from a developer point of view: build targets, runtime behavior, environment, dependency management, customization points, quality gates, and troubleshooting.

## Stack Overview

- **Base OS**: Ubuntu 22.04
- **GPU stack**:
  - Regular image: CUDA 12.4, stable PyTorch via upstream requirements
  - RTX 5090 image: CUDA 12.8, PyTorch Nightly (explicit cu128 wheels)
- **Python**: 3.12 (set as system default inside the image)
- **Package manager**: pip + uv (uv used for fast installs; `UV_LINK_MODE=copy`)
- **Tools bundled**: FileBrowser (port 8080), JupyterLab (port 8888), OpenSSH server (port 22), FFmpeg (NVENC), common CLI tools
- **Primary app**: ComfyUI, with pre-installed custom nodes

## Repository Layout

- `Dockerfile` – Regular image (CUDA 12.4)
- `Dockerfile.5090` – RTX 5090 image (CUDA 12.8 + PyTorch cu128)
- `start.sh` – Runtime bootstrap for regular image
- `start.5090.sh` – Runtime bootstrap for 5090 image
- `docker-bake.hcl` – Buildx bake targets (`regular`, `dev`, `rtx5090`)
- `README.md` – User-facing overview
- `docs/conventions.md` – This document

At runtime, the container uses:

- `/workspace/runpod-slim/ComfyUI` – ComfyUI checkout and virtual environment
- `/workspace/runpod-slim/comfyui_args.txt` – Optional line-delimited ComfyUI args
- `/workspace/runpod-slim/filebrowser.db` – FileBrowser DB

## Build Targets

Use Docker Buildx Bake with the provided HCL file.

- `regular` (default production):
  - Dockerfile: `Dockerfile`
  - Tag: `runpod/comfyui:${TAG}` (defaults to `slim`)
  - Platform: `linux/amd64`
- `dev` (local testing):
  - Dockerfile: `Dockerfile`
  - Tag: `runpod/comfyui:dev`
  - Output: local docker image (not pushed)
- `rtx5090` (CUDA 12.8 + latest torch):
  - Dockerfile: `Dockerfile.5090`
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

Startup is handled by `start.sh` (or `start.5090.sh` for the 5090 image):

- Initializes SSH server. If `PUBLIC_KEY` is set, it is added to `~/.ssh/authorized_keys`; otherwise a random root password is generated and printed to logs.
- Exports selected env vars broadly to `/etc/environment`, PAM, and `~/.ssh/environment` for non-interactive shells.
- Initializes and starts FileBrowser on port 8080 (root `/workspace`). Default admin user is created on first run.
- Starts JupyterLab on port 8888, root at `/workspace`. Token set via `JUPYTER_PASSWORD` if provided.
- Ensures `comfyui_args.txt` exists.
- Clones ComfyUI and preselected custom nodes on first run, then creates a Python 3.12 venv and installs dependencies using `uv`.
- Starts ComfyUI with fixed args `--listen 0.0.0.0 --port 8188` plus any custom args from `comfyui_args.txt`.

Differences in 5090 script:

- Virtualenv path: `.venv-cu128`
- Masks torch-related lines in ComfyUI `requirements.txt` and installs torch/cu128 wheels explicitly: `torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu128`.

## Ports

- 8188 – ComfyUI
- 8189 – Helper web UI (logs + model downloaders)
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
  - Regular: `/workspace/runpod-slim/ComfyUI/.venv`
  - 5090: `/workspace/runpod-slim/ComfyUI/.venv-cu128`
- `uv` is used for dependency installation for speed and reproducibility.
- Regular image installs ComfyUI `requirements.txt` as-is.
- 5090 image comments out torch-related requirements and installs CUDA 12.8 torch wheels explicitly.
- Custom nodes: repos are cloned into `ComfyUI/custom_nodes/`. On first run and subsequent starts, the script attempts to install each node’s `requirements.txt`, run `install.py`, or `setup.py` if present.

Preinstalled custom nodes (initial set):

- `ComfyUI-Manager` (ltdrdata)
- `ComfyUI-KJNodes` (kijai)
- `Civicomfy` (MoonGoblinDev)

## Customization Points

- `comfyui_args.txt` – Add one CLI arg per line; comments starting with `#` are ignored. These are appended after fixed args.
- Add/remove custom nodes by editing the `CUSTOM_NODES` array in the start script(s), or pre-baking them into the image.
- Additional system packages: modify the respective Dockerfile `apt-get install` lines.
- Python packages: extend installation blocks in the start script after venv activation. Prefer `uv pip install --no-cache ...`.

## Dev Conventions

- Keep images lean. Prefer runtime install via `uv` over baking large wheels unless required (e.g., 5090 torch wheels).
- Avoid changing ports; they are referenced by external templates (RunPod/UI tooling).
- Use Python 3.12. Do not downgrade in scripts.
- When adding new env vars needed by downstream processes, ensure they are exported in `export_env_vars()` the same way as others.
- For new custom nodes, ensure idempotent installs: the loop checks for `requirements.txt`, `install.py`, and `setup.py`.
- Shell scripting: keep `set -e` at top; prefer explicit guards; write idempotent steps safe to re-run.

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
  - Check `/workspace/runpod-slim/comfyui.log` (tailing in foreground).
  - Ensure `comfyui_args.txt` doesn’t contain invalid flags (comments with `#` are okay).
- JupyterLab auth:
  - If `JUPYTER_PASSWORD` is unset, Jupyter may allow tokenless or default behavior. Set it explicitly if needed.
- SSH access:
  - If no `PUBLIC_KEY` is provided, a random root password is generated and printed to stdout. Check container logs.
  - Ensure port 22 is mapped from the host, e.g., `-p 2222:22`.
- GPU/torch issues on 5090 image:
  - Verify you’re running the `-5090` tag.
  - Torch builds are installed from `https://download.pytorch.org/whl/cu128`; confirm compatibility with the host driver.

## Release & Tagging

- Default tag base is `slim` via `TAG` in `docker-bake.hcl`.
- For 5090 builds, the pushed tag is `${TAG}-5090`.
- Keep `README.md` ports and features in sync when changing defaults.

## License

- GPLv3 as per `LICENSE`.
