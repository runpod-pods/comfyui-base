#!/bin/bash
set -e  # Exit the script if any statement returns a non-true return value

COMFYUI_DIR="/workspace/runpod-slim/ComfyUI"
VENV_DIR="$COMFYUI_DIR/.venv-cu128"
OLD_VENV_DIR="$COMFYUI_DIR/.venv"
FILEBROWSER_CONFIG="/root/.config/filebrowser/config.json"
DB_FILE="/workspace/runpod-slim/filebrowser.db"

# ---------------------------------------------------------------------------- #
#                          Function Definitions                                  #
# ---------------------------------------------------------------------------- #

# Setup SSH with optional key or random password
setup_ssh() {
    mkdir -p ~/.ssh
    
    if [ ! -f /etc/ssh/ssh_host_ed25519_key ]; then
        ssh-keygen -A -q
    fi

    # If PUBLIC_KEY is provided, use it
    if [[ $PUBLIC_KEY ]]; then
        echo "$PUBLIC_KEY" >> ~/.ssh/authorized_keys
        chmod 700 -R ~/.ssh
    else
        # Generate random password if no public key
        RANDOM_PASS=$(openssl rand -base64 12)
        echo "root:${RANDOM_PASS}" | chpasswd
        echo "Generated random SSH password for root: ${RANDOM_PASS}"
    fi

    # Configure SSH to preserve environment variables
    echo "PermitUserEnvironment yes" >> /etc/ssh/sshd_config

    # Start SSH service
    /usr/sbin/sshd
}

# Export environment variables
export_env_vars() {
    echo "Exporting environment variables..."
    
    # Create environment files
    ENV_FILE="/etc/environment"
    PAM_ENV_FILE="/etc/security/pam_env.conf"
    SSH_ENV_DIR="/root/.ssh/environment"
    
    # Backup original files
    cp "$ENV_FILE" "${ENV_FILE}.bak" 2>/dev/null || true
    cp "$PAM_ENV_FILE" "${PAM_ENV_FILE}.bak" 2>/dev/null || true
    
    # Clear files
    > "$ENV_FILE"
    > "$PAM_ENV_FILE"
    mkdir -p /root/.ssh
    > "$SSH_ENV_DIR"
    
    # Export to multiple locations for maximum compatibility
    printenv | grep -E '^RUNPOD_|^PATH=|^_=|^CUDA|^LD_LIBRARY_PATH|^PYTHONPATH' | while read -r line; do
        # Get variable name and value
        name=$(echo "$line" | cut -d= -f1)
        value=$(echo "$line" | cut -d= -f2-)
        
        # Add to /etc/environment (system-wide)
        echo "$name=\"$value\"" >> "$ENV_FILE"
        
        # Add to PAM environment
        echo "$name DEFAULT=\"$value\"" >> "$PAM_ENV_FILE"
        
        # Add to SSH environment file
        echo "$name=\"$value\"" >> "$SSH_ENV_DIR"
        
        # Add to current shell
        echo "export $name=\"$value\"" >> /etc/rp_environment
    done
    
    # Add sourcing to shell startup files
    echo 'source /etc/rp_environment' >> ~/.bashrc
    echo 'source /etc/rp_environment' >> /etc/bash.bashrc
    
    # Set permissions
    chmod 644 "$ENV_FILE" "$PAM_ENV_FILE"
    chmod 600 "$SSH_ENV_DIR"
}

# Start Jupyter Lab server for remote access
start_jupyter() {
    mkdir -p /workspace
    echo "Starting Jupyter Lab on port 8888..."
    nohup jupyter lab \
        --allow-root \
        --no-browser \
        --port=8888 \
        --ip=0.0.0.0 \
        --FileContentsManager.delete_to_trash=False \
        --FileContentsManager.preferred_dir=/workspace \
        --ServerApp.root_dir=/workspace \
        --ServerApp.terminado_settings='{"shell_command":["/bin/bash"]}' \
        --IdentityProvider.token="${JUPYTER_PASSWORD:-}" \
        --ServerApp.allow_origin=* &> /jupyter.log &
    echo "Jupyter Lab started"
}

# ---------------------------------------------------------------------------- #
#                               Main Program                                     #
# ---------------------------------------------------------------------------- #

# Setup environment
setup_ssh
export_env_vars

# Initialize FileBrowser if not already done
if [ ! -f "$DB_FILE" ]; then
    echo "Initializing FileBrowser..."
    filebrowser config init
    filebrowser config set --address 0.0.0.0
    filebrowser config set --port 8080
    filebrowser config set --root /workspace
    filebrowser config set --auth.method=json
    filebrowser users add admin adminadmin12 --perm.admin
else
    echo "Using existing FileBrowser configuration..."
fi

# Start FileBrowser
echo "Starting FileBrowser on port 8080..."
nohup filebrowser &> /filebrowser.log &

start_jupyter

# Create default comfyui_args.txt if it doesn't exist
ARGS_FILE="/workspace/runpod-slim/comfyui_args.txt"
if [ ! -f "$ARGS_FILE" ]; then
    echo "# Add your custom ComfyUI arguments here (one per line)" > "$ARGS_FILE"
    echo "Created empty ComfyUI arguments file at $ARGS_FILE"
fi

# Migrate old CUDA 12.4 venv to cu128
if [ -d "$OLD_VENV_DIR" ] && [ ! -d "$VENV_DIR" ]; then
    NODE_COUNT=$(find "$COMFYUI_DIR/custom_nodes" -maxdepth 2 -name "requirements.txt" 2>/dev/null | wc -l)
    echo "============================================="
    echo "  CUDA 12.4 -> 12.8 migration"
    echo "  Reinstalling deps for $NODE_COUNT custom nodes"
    echo "  This may take several minutes"
    echo "============================================="
    mv "$OLD_VENV_DIR" "${OLD_VENV_DIR}.bak"
    cd "$COMFYUI_DIR"
    python3.12 -m venv --system-site-packages "$VENV_DIR"
    source "$VENV_DIR/bin/activate"
    python -m ensurepip
    # Skip nodes baked into the image — their deps are in system site-packages
    BAKED_NODES="ComfyUI-Manager ComfyUI-KJNodes Civicomfy ComfyUI-RunpodDirect"
    CURRENT=0
    INSTALLED=0
    for req in "$COMFYUI_DIR"/custom_nodes/*/requirements.txt; do
        if [ -f "$req" ]; then
            NODE_NAME=$(basename "$(dirname "$req")")
            case " $BAKED_NODES " in
                *" $NODE_NAME "*) continue ;;
            esac
            CURRENT=$((CURRENT + 1))
            echo "[$CURRENT] $NODE_NAME"
            pip install -r "$req" 2>&1 | grep -E "^(Successfully|ERROR)" || true
            INSTALLED=$((INSTALLED + 1))
        fi
    done
    echo "Upgrading ComfyUI requirements..."
    pip install --upgrade -r "$COMFYUI_DIR/requirements.txt" 2>&1 | grep -E "^(Successfully|ERROR)" || true
    echo "Migration complete — $INSTALLED user nodes processed (${NODE_COUNT} total, baked nodes skipped)"
    echo "Old venv backed up at ${OLD_VENV_DIR}.bak — delete it to free space:"
    echo "  rm -rf ${OLD_VENV_DIR}.bak"
fi

# Setup ComfyUI if needed
if [ ! -d "$COMFYUI_DIR" ] || [ ! -d "$VENV_DIR" ]; then
    echo "First time setup: Copying baked ComfyUI to workspace..."

    # Copy baked ComfyUI from image (no git, no network)
    if [ ! -d "$COMFYUI_DIR" ]; then
        cp -r /opt/comfyui-baked "$COMFYUI_DIR"
        echo "ComfyUI copied to workspace"
    fi

    # Create venv with access to system packages (torch, numpy, etc. pre-installed in image)
    if [ ! -d "$VENV_DIR" ]; then
        cd "$COMFYUI_DIR"
        python3.12 -m venv --system-site-packages "$VENV_DIR"
        source "$VENV_DIR/bin/activate"

        # Ensure pip is available in the venv (needed for ComfyUI-Manager)
        python -m ensurepip

        echo "Base packages (torch, numpy, etc.) available from system site-packages"
        echo "ComfyUI ready — all dependencies pre-installed in image"
    fi
else
    # Just activate the existing venv
    source "$VENV_DIR/bin/activate"
    echo "Using existing ComfyUI installation"
fi

# Warm up pip so ComfyUI-Manager's 5s timeout check doesn't fail on cold start
python -m pip --version > /dev/null 2>&1

# Start ComfyUI — keep container alive if it crashes so SSH/Jupyter remain accessible
cd $COMFYUI_DIR
FIXED_ARGS="--listen 0.0.0.0 --port 8188"
if [ -s "$ARGS_FILE" ]; then
    CUSTOM_ARGS=$(grep -v '^#' "$ARGS_FILE" | tr '\n' ' ')
    if [ ! -z "$CUSTOM_ARGS" ]; then
        FIXED_ARGS="$FIXED_ARGS $CUSTOM_ARGS"
    fi
fi

echo "Starting ComfyUI with args: $FIXED_ARGS"
python main.py $FIXED_ARGS &
COMFY_PID=$!
trap "kill $COMFY_PID 2>/dev/null" SIGTERM SIGINT
wait $COMFY_PID || true

echo "============================================="
echo "  ComfyUI crashed — check the logs above."
echo "  SSH and JupyterLab are still available."
echo "  To restart after fixing:"
echo "    cd $COMFYUI_DIR && source .venv-cu128/bin/activate"
echo "    python main.py $FIXED_ARGS"
echo "============================================="

sleep infinity
