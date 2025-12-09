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

# Check if huggingface-cli is installed
if ! command -v huggingface-cli &> /dev/null; then
    echo -e "${YELLOW}Installing huggingface-cli...${NC}"
    pip install -U "huggingface_hub[cli]"
fi

# Create directories
mkdir -p "$COMFYUI_DIR/models/checkpoints"
mkdir -p "$COMFYUI_DIR/models/unet"
mkdir -p "$COMFYUI_DIR/models/vae"
mkdir -p "$COMFYUI_DIR/models/text_encoders"
mkdir -p "$COMFYUI_DIR/models/loras"
mkdir -p "$COMFYUI_DIR/models/controlnet"
mkdir -p "$COMFYUI_DIR/models/diffusion_models"

echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}  1. Downloading Z-Image-Turbo Models${NC}"
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"

# Z-Image-Turbo Main Model
echo -e "${YELLOW}📦 Downloading Z-Image-Turbo main model (BF16, sharded)...${NC}"
huggingface-cli download Tongyi-MAI/Z-Image-Turbo \
    --include "transformer/*" "model_index.json" \
    --local-dir "$COMFYUI_DIR/models/diffusion_models/Z-Image-Turbo"

# Z-Image ControlNet Union
echo -e "${YELLOW}📦 Downloading Z-Image ControlNet Union...${NC}"
huggingface-cli download alibaba-pai/Z-Image-Turbo-Fun-Controlnet-Union \
    Z-Image-Turbo-Fun-Controlnet-Union.safetensors \
    --local-dir "$COMFYUI_DIR/models/controlnet"

# Z-Image LoRAs
echo -e "${YELLOW}📦 Downloading Z-Image De-Turbo LoRA (BF16)...${NC}"
huggingface-cli download ostris/Z-Image-De-Turbo \
    z_image_de_turbo_v1_bf16.safetensors \
    --local-dir "$COMFYUI_DIR/models/loras"

echo -e "${YELLOW}📦 Downloading Z-Image AIO LoRA...${NC}"
huggingface-cli download SeeSee21/Z-Image-Turbo-AIO \
    z-image-turbo-bf16-aio.safetensors \
    --local-dir "$COMFYUI_DIR/models/loras"

# Z-Image VAE
echo -e "${YELLOW}📦 Downloading Z-Image VAE...${NC}"
huggingface-cli download Tongyi-MAI/Z-Image-Turbo \
    vae/diffusion_pytorch_model.safetensors \
    --local-dir /tmp/z-image-vae
mv /tmp/z-image-vae/vae/diffusion_pytorch_model.safetensors \
    "$COMFYUI_DIR/models/vae/z_image_vae.safetensors"
rm -rf /tmp/z-image-vae

# Z-Image Text Encoder
echo -e "${YELLOW}📦 Downloading Z-Image Text Encoder (BF16, sharded)...${NC}"
huggingface-cli download Tongyi-MAI/Z-Image-Turbo \
    --include "text_encoder/*" \
    --local-dir "$COMFYUI_DIR/models/text_encoders/z-image-turbo"

echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}  2. Downloading Qwen-Image-Edit-2509 Models (Quantized)${NC}"
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"

# Qwen-Image-Edit Q5_0 GGUF (RTX 5090 optimized)
echo -e "${YELLOW}📦 Downloading Qwen-Image-Edit-2509-Q5_0.gguf (14.4 GB)...${NC}"
huggingface-cli download QuantStack/Qwen-Image-Edit-2509-GGUF \
    Qwen-Image-Edit-2509-Q5_0.gguf \
    --local-dir "$COMFYUI_DIR/models/unet"

# Qwen-Image VAE
echo -e "${YELLOW}📦 Downloading Qwen-Image VAE...${NC}"
huggingface-cli download Qwen/Qwen-Image-Edit-2509 \
    vae/diffusion_pytorch_model.safetensors \
    --local-dir /tmp/qwen-vae
mv /tmp/qwen-vae/vae/diffusion_pytorch_model.safetensors \
    "$COMFYUI_DIR/models/vae/qwen_image_vae.safetensors"
rm -rf /tmp/qwen-vae

# Qwen2.5-VL-7B FP8 Text Encoder
echo -e "${YELLOW}📦 Downloading Qwen2.5-VL-7B FP8 text encoder...${NC}"
huggingface-cli download nvidia/Qwen2.5-VL-7B-Instruct-FP8 \
    model.safetensors \
    --local-dir /tmp/qwen-te-fp8
mv /tmp/qwen-te-fp8/model.safetensors \
    "$COMFYUI_DIR/models/text_encoders/qwen_2.5_vl_7b_fp8_scaled.safetensors"
rm -rf /tmp/qwen-te-fp8

# Qwen3-8B CLIP Model
echo -e "${YELLOW}📦 Downloading Qwen3-8B CLIP model...${NC}"
huggingface-cli download Qwen/Qwen3-8B \
    --local-dir "$COMFYUI_DIR/models/text_encoders/qwen3-8b"

# Qwen-Image-Edit Lightning LoRAs
echo -e "${YELLOW}📦 Downloading Qwen-Image-Edit Lightning 4-step LoRA...${NC}"
huggingface-cli download lightx2v/Qwen-Image-Lightning \
    Qwen-Image-Edit-2509/Qwen-Image-Edit-2509-Lightning-4steps-V1.0-bf16.safetensors \
    --local-dir /tmp/lightning-lora
mv /tmp/lightning-lora/Qwen-Image-Edit-2509/Qwen-Image-Edit-2509-Lightning-4steps-V1.0-bf16.safetensors \
    "$COMFYUI_DIR/models/loras/"

echo -e "${YELLOW}📦 Downloading Qwen-Image-Edit Lightning 8-step LoRA...${NC}"
huggingface-cli download lightx2v/Qwen-Image-Lightning \
    Qwen-Image-Edit-2509/Qwen-Image-Edit-2509-Lightning-8steps-V1.0-bf16.safetensors \
    --local-dir /tmp/lightning-lora
mv /tmp/lightning-lora/Qwen-Image-Edit-2509/Qwen-Image-Edit-2509-Lightning-8steps-V1.0-bf16.safetensors \
    "$COMFYUI_DIR/models/loras/"
rm -rf /tmp/lightning-lora

# Qwen-Image-Edit Popular LoRAs
echo -e "${YELLOW}📦 Downloading Qwen-Image-Edit popular LoRAs...${NC}"

echo -e "${YELLOW}  - Next-Scene-v2 LoRA...${NC}"
huggingface-cli download lovis93/next-scene-qwen-image-lora-2509 \
    next-scene_lora-v2-3000.safetensors \
    --local-dir "$COMFYUI_DIR/models/loras"

echo -e "${YELLOW}  - Multiple-Angles LoRA...${NC}"
huggingface-cli download dx8152/Qwen-Edit-2509-Multiple-angles \
    Qwen-Edit-2509-Multiple-angles.safetensors \
    --local-dir "$COMFYUI_DIR/models/loras"

echo -e "${YELLOW}  - Light-Migration LoRA...${NC}"
huggingface-cli download dx8152/Qwen-Edit-2509-Light-Migration \
    Qwen-Edit-2509-Light-Migration.safetensors \
    --local-dir "$COMFYUI_DIR/models/loras"

echo -e "${YELLOW}  - Best-Face-Swap LoRA...${NC}"
huggingface-cli download Alissonerdx/BFS-Best-Face-Swap \
    BFS-Best-Face-Swap.safetensors \
    --local-dir "$COMFYUI_DIR/models/loras"

echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}  3. Downloading Juggernaut XL Ragnarok (from CivitAI)${NC}"
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"

# CivitAI download requires API key: export CIVITAI_API_KEY=your_key
if [ -z "${CIVITAI_API_KEY:-}" ]; then
    echo -e "${YELLOW}CIVITAI_API_KEY not set. Skipping Juggernaut XL Ragnarok download.${NC}"
else
    echo -e "${YELLOW}📦 Downloading Juggernaut XL Ragnarok checkpoint (6.46 GB) from CivitAI...${NC}"
    curl -L \
        -H "Authorization: Bearer ${CIVITAI_API_KEY}" \
        -o /tmp/juggernautXL_ragnarokBy.safetensors \
        "https://civitai.com/api/download/models/1759168"
    mv /tmp/juggernautXL_ragnarokBy.safetensors \
        "$COMFYUI_DIR/models/checkpoints/juggernautXL_ragnarokBy.safetensors"
fi

echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}  4. Installing ComfyUI-GGUF Custom Node${NC}"
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"

# Install ComfyUI-GGUF for Q5_0 model support
if [ ! -d "$COMFYUI_DIR/custom_nodes/ComfyUI-GGUF" ]; then
    echo -e "${YELLOW}📦 Cloning ComfyUI-GGUF...${NC}"
    cd "$COMFYUI_DIR/custom_nodes"
    git clone https://github.com/city96/ComfyUI-GGUF
    cd ComfyUI-GGUF
    
    # Activate venv if exists
    if [ -d "$COMFYUI_DIR/.venv-cu128" ]; then
        source "$COMFYUI_DIR/.venv-cu128/bin/activate"
    fi
    
    if [ -f "requirements.txt" ]; then
        pip install -r requirements.txt
    fi
else
    echo -e "${GREEN}✓ ComfyUI-GGUF already installed${NC}"
fi

echo ""
echo -e "${GREEN}╔════════════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║                    Download Complete!                          ║${NC}"
echo -e "${GREEN}╚════════════════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "${GREEN}✓ Z-Image-Turbo models downloaded${NC}"
echo -e "${GREEN}✓ Qwen-Image-Edit-2509 (Q5_0 GGUF) downloaded${NC}"
echo -e "${GREEN}✓ Juggernaut XL Ragnarok downloaded${NC}"
echo -e "${GREEN}✓ All LoRAs and supporting files downloaded${NC}"
echo ""
echo -e "${YELLOW}📊 Total disk usage:${NC}"
du -sh "$COMFYUI_DIR/models" 2>/dev/null || echo "Unable to calculate"
echo ""
echo -e "${GREEN}🚀 Ready to start ComfyUI!${NC}"
echo -e "${YELLOW}Run: bash start.5090.sh${NC}"
echo ""
