[![Watch the video](https://i3.ytimg.com/vi/JovhfHhxqdM/hqdefault.jpg)](https://www.youtube.com/watch?v=JovhfHhxqdM)

Run the latest ComfyUI with CUDA 13. All dependencies are pre-installed in the image. On first boot, ComfyUI is copied to your workspace — when you see `[ComfyUI-Manager] All startup tasks have been completed.` in the logs, it's ready to use.

## Upgrading from a previous version

If you have an existing pod created with an older version of this template (CUDA 12.4), a one-time migration of the virtual environment will be performed automatically on the next boot to make it compatible with CUDA 13. This may take a few extra minutes on first start after the update.

## Access

- `8188`: ComfyUI web UI
- `8080`: FileBrowser (admin / adminadmin12)
- `8888`: JupyterLab (token via `JUPYTER_PASSWORD`, root at `/workspace`)
- `22`: SSH (set `PUBLIC_KEY` or check logs for generated root password)

## Pre-installed custom nodes

- ComfyUI-Manager
- ComfyUI-KJNodes
- Civicomfy
- ComfyUI-RunpodDirect

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
