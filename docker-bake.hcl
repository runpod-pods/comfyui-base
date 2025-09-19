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
  dockerfile = "Dockerfile"
  tags = ["runpod/comfyui:${TAG}"]
}

# Dev image for local testing
target "dev" {
  inherits = ["common"]
  dockerfile = "Dockerfile"
  tags = ["runpod/comfyui:dev"]
  output = ["type=docker"]
}

# RTX 5090 optimized image (CUDA 12.8 + latest PyTorch build)
target "rtx5090" {
  inherits = ["common"]
  dockerfile = "Dockerfile.5090"
  args = {
    START_SCRIPT = "start.5090.sh"
  }
  tags = ["runpod/comfyui:${TAG}-5090"]
}
