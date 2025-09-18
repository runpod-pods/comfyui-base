variable "TAG" {
  default = "slim"
}

# Common settings for all targets
target "common" {
  context = "."
  platforms = ["linux/amd64"]
  args = {
    BUILDKIT_INLINE_CACHE = "1"
  }
}

# Regular ComfyUI image (CUDA 12.4)
target "regular" {
  inherits = ["common"]
  dockerfile = "docker/Dockerfile"
  tags = ["runpod/comfyui-slim:${TAG}"]
}

# RTX 5090 optimized image (CUDA 12.8)
target "rtx5090" {
  inherits = ["common"]
  dockerfile = "docker/Dockerfile.5090"
  args = {
    START_SCRIPT = "scripts/start.5090.sh"
  }
  tags = ["runpod/comfyui-slim:${TAG}-5090"]
}
