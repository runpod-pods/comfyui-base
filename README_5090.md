[![Watch the video](https://i3.ytimg.com/vi/JovhfHhxqdM/hqdefault.jpg)](https://www.youtube.com/watch?v=JovhfHhxqdM)

Run the latest ComfyUI optimized for NVIDIA Blackwell architecture (RTX 5090, B200). First start installs dependencies (takes a few minutes), then when you see this in the logs, ComfyUI is ready to be used: `[ComfyUI-Manager] All startup tasks have been completed.`

## Access

- `8188`: ComfyUI web UI
- `8080`: FileBrowser (admin / adminadmin12)
- `8888`: JupyterLab (token via `JUPYTER_PASSWORD`, root at `/workspace`)
- `22`: SSH (set `PUBLIC_KEY` or check logs for generated root password)

## Pre-installed custom nodes

- ComfyUI-Manager
- ComfyUI-KJNodes
- Civicomfy

## Source Code

This is an open source template. Source code available at: [github.com/runpod-workers/comfyui-base](https://github.com/runpod-workers/comfyui-base)

## Custom Arguments

Edit `/workspace/runpod-slim/comfyui_args.txt` (one arg per line):

```
--max-batch-size 8
--preview-method auto
```

## Directory Structure

- `/workspace/runpod-slim/ComfyUI`: ComfyUI install
- `/workspace/runpod-slim/comfyui_args.txt`: ComfyUI args
- `/workspace/runpod-slim/filebrowser.db`: FileBrowser DB
