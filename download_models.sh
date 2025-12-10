#!/bin/bash
set -e  # Exit on error

# Color output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

COMFYUI_DIR="/workspace/runpod-slim/ComfyUI"

echo -e "${GREEN}╔════════════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║          ComfyUI Model Downloader for RTX 5090                ║${NC}"
echo -e "${GREEN}╚════════════════════════════════════════════════════════════════╝${NC}"
echo ""

# Check if ComfyUI is installed
if [ ! -f "$COMFYUI_DIR/main.py" ]; then
    echo -e "${RED}Error: ComfyUI is not installed at $COMFYUI_DIR${NC}"
    echo -e "${RED}Please install ComfyUI first before downloading models.${NC}"
    exit 1
fi

# Check if huggingface-cli is installed
if ! command -v huggingface-cli &> /dev/null; then
    echo -e "${YELLOW}Installing huggingface-cli...${NC}"
    pip install -U "huggingface_hub[cli]"
fi

# Create model directories only when needed
mkdir -p "$COMFYUI_DIR/models/checkpoints"
mkdir -p "$COMFYUI_DIR/models/vae"
mkdir -p "$COMFYUI_DIR/models/text_encoders"
mkdir -p "$COMFYUI_DIR/models/loras"
mkdir -p "$COMFYUI_DIR/models/controlnet"
mkdir -p "$COMFYUI_DIR/models/diffusion_models"

echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}  1. Downloading Z-Image-Turbo Models${NC}"
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"

# Z-Image-Turbo Main Model
echo -e "${YELLOW}📦 Downloading Z-Image-Turbo main model (BF16)...${NC}"
hf download Comfy-Org/z_image_turbo \
    split_files/diffusion_models/z_image_turbo_bf16.safetensors \
    --local-dir /tmp/z-image-models
mv /tmp/z-image-models/split_files/diffusion_models/z_image_turbo_bf16.safetensors \
    "$COMFYUI_DIR/models/diffusion_models/"
rm -rf /tmp/z-image-models

# Z-Image ControlNet Union
echo -e "${YELLOW}📦 Downloading Z-Image ControlNet Union...${NC}"
hf download alibaba-pai/Z-Image-Turbo-Fun-Controlnet-Union \
    Z-Image-Turbo-Fun-Controlnet-Union.safetensors \
    --local-dir "$COMFYUI_DIR/models/controlnet"

# Z-Image VAE (Flux 1 VAE)
echo -e "${YELLOW}📦 Downloading Z-Image VAE (ae.safetensors)...${NC}"
hf download Comfy-Org/z_image_turbo \
    split_files/vae/ae.safetensors \
    --local-dir /tmp/z-image-models
mv /tmp/z-image-models/split_files/vae/ae.safetensors \
    "$COMFYUI_DIR/models/vae/"
rm -rf /tmp/z-image-models

# Z-Image Text Encoder
echo -e "${YELLOW}📦 Downloading Z-Image Text Encoder (Qwen3-4B)...${NC}"
hf download Comfy-Org/z_image_turbo \
    split_files/text_encoders/qwen_3_4b.safetensors \
    --local-dir /tmp/z-image-models
mv /tmp/z-image-models/split_files/text_encoders/qwen_3_4b.safetensors \
    "$COMFYUI_DIR/models/text_encoders/"
rm -rf /tmp/z-image-models

echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}  2. Downloading Qwen-Image-Edit-2509 Models (Quantized)${NC}"
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"

# Qwen-Image-Edit-2509 Main Model (FP8)
echo -e "${YELLOW}📦 Downloading Qwen-Image-Edit-2509 (FP8)...${NC}"
hf download Comfy-Org/Qwen-Image-Edit_ComfyUI \
    split_files/diffusion_models/qwen_image_edit_2509_fp8_e4m3fn.safetensors \
    --local-dir /tmp/qwen-models
mv /tmp/qwen-models/split_files/diffusion_models/qwen_image_edit_2509_fp8_e4m3fn.safetensors \
    "$COMFYUI_DIR/models/diffusion_models/"
rm -rf /tmp/qwen-models

# Qwen-Image VAE
echo -e "${YELLOW}📦 Downloading Qwen-Image VAE...${NC}"
hf download Comfy-Org/Qwen-Image_ComfyUI \
    split_files/vae/qwen_image_vae.safetensors \
    --local-dir /tmp/qwen-models
mv /tmp/qwen-models/split_files/vae/qwen_image_vae.safetensors \
    "$COMFYUI_DIR/models/vae/"
rm -rf /tmp/qwen-models

# Qwen2.5-VL-7B FP8 Text Encoder
echo -e "${YELLOW}📦 Downloading Qwen2.5-VL-7B FP8 text encoder...${NC}"
hf download Comfy-Org/Qwen-Image_ComfyUI \
    split_files/text_encoders/qwen_2.5_vl_7b_fp8_scaled.safetensors \
    --local-dir /tmp/qwen-models
mv /tmp/qwen-models/split_files/text_encoders/qwen_2.5_vl_7b_fp8_scaled.safetensors \
    "$COMFYUI_DIR/models/text_encoders/"
rm -rf /tmp/qwen-models

# Qwen-Image-Edit Lightning LoRAs
echo -e "${YELLOW}📦 Downloading Qwen-Image-Edit Lightning 4-step LoRA...${NC}"
hf download lightx2v/Qwen-Image-Lightning \
    Qwen-Image-Edit-2509/Qwen-Image-Edit-2509-Lightning-4steps-V1.0-bf16.safetensors \
    --local-dir /tmp/lightning-lora
mv /tmp/lightning-lora/Qwen-Image-Edit-2509/Qwen-Image-Edit-2509-Lightning-4steps-V1.0-bf16.safetensors \
    "$COMFYUI_DIR/models/loras/"

echo -e "${YELLOW}📦 Downloading Qwen-Image-Edit Lightning 8-step LoRA...${NC}"
hf download lightx2v/Qwen-Image-Lightning \
    Qwen-Image-Edit-2509/Qwen-Image-Edit-2509-Lightning-8steps-V1.0-bf16.safetensors \
    --local-dir /tmp/lightning-lora
mv /tmp/lightning-lora/Qwen-Image-Edit-2509/Qwen-Image-Edit-2509-Lightning-8steps-V1.0-bf16.safetensors \
    "$COMFYUI_DIR/models/loras/"
rm -rf /tmp/lightning-lora

# Qwen-Image-Edit Popular LoRAs
echo -e "${YELLOW}📦 Downloading Qwen-Image-Edit popular LoRAs...${NC}"

echo -e "${YELLOW}  - Next-Scene-v2 LoRA...${NC}"
hf download lovis93/next-scene-qwen-image-lora-2509 \
    next-scene_lora-v2-3000.safetensors \
    --local-dir "$COMFYUI_DIR/models/loras"

echo -e "${YELLOW}  - Multiple-Angles LoRA...${NC}"
hf download dx8152/Qwen-Edit-2509-Multiple-angles \
    镜头转换.safetensors \
    --local-dir "$COMFYUI_DIR/models/loras"

echo -e "${YELLOW}  - Light-Migration LoRA...${NC}"
hf download dx8152/Qwen-Edit-2509-Light-Migration \
    参考色调.safetensors \
    --local-dir "$COMFYUI_DIR/models/loras"

echo -e "${YELLOW}  - Best-Face-Swap LoRA (Face v1)...${NC}"
hf download Alissonerdx/BFS-Best-Face-Swap \
    bfs_face_v1_qwen_image_edit_2509.safetensors \
    --local-dir "$COMFYUI_DIR/models/loras"

echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}  3. Downloading Juggernaut XL Ragnarok (from CivitAI)${NC}"
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"

# CivitAI download requires API key: export CIVITAI_API_KEY=your_key
echo -e "${YELLOW}📦 Downloading Juggernaut XL Ragnarok checkpoint (6.46 GB) from CivitAI...${NC}"
curl -L \
    -o "$COMFYUI_DIR/models/checkpoints/juggernautXL_ragnarokBy.safetensors" \
    "https://civitai.com/api/download/models/1759168"

echo ""
echo -e "${GREEN}╔════════════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║                    Download Complete!                          ║${NC}"
echo -e "${GREEN}╚════════════════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "${GREEN}✓ Z-Image-Turbo models downloaded${NC}"
echo -e "${GREEN}✓ Qwen-Image-Edit-2509 (FP8) downloaded${NC}"
echo -e "${GREEN}✓ Juggernaut XL Ragnarok downloaded${NC}"
echo -e "${GREEN}✓ All LoRAs and supporting files downloaded${NC}"
echo ""
echo -e "${YELLOW}📊 Total disk usage:${NC}"
du -sh "$COMFYUI_DIR/models" 2>/dev/null || echo "Unable to calculate"
echo ""
echo -e "${GREEN}🚀 Ready to start ComfyUI!${NC}"
echo ""
