# ComfyUI

A compact and optimized Docker container designed as an easy-to-use RunPod template for ComfyUI. Images are highly optimized for size, only ~650MB while including all features!

## Why ComfyUI?

- Purpose-built for RunPod deployments
- Ultra-compact: Only ~650MB image size (compared to multi-GB alternatives)
- Zero configuration needed: Works out of the box
- Includes all essential tools for remote work

## Features

- Two optimized variants:
  - Regular: CUDA 12.4 with stable PyTorch
  - RTX 5090: CUDA 12.8 with PyTorch Nightly (optimized for latest NVIDIA GPUs)
- Built-in tools:
  - FileBrowser for easy file management (port 8080)
  - JupyterLab workspace (port 8048)
  - SSH access
- Pre-installed custom nodes:
  - ComfyUI-Manager
  - ComfyUI-Crystools
  - ComfyUI-KJNodes
- Performance optimizations:
  - UV package installer for faster dependency installation
  - NVENC support in FFmpeg
  - Optimized CUDA configurations

## Ports

- `8188`: ComfyUI web interface
- `8080`: FileBrowser interface
- `8888`: JupyterLab interface
- `22`: SSH access

## Custom Arguments

You can customize ComfyUI startup arguments by editing `/workspace/runpod-slim/comfyui_args.txt`. Add one argument per line:

```
--max-batch-size 8
--preview-method auto
```

## Directory Structure

- `/workspace/runpod-slim/ComfyUI`: Main ComfyUI installation
- `/workspace/runpod-slim/comfyui_args.txt`: Custom arguments file
- `/workspace/runpod-slim/filebrowser.db`: FileBrowser database

## License

This project is licensed under the GPLv3 License.
