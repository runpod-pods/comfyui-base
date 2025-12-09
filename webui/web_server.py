import argparse
import json
import os
import shutil
import threading
import time
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen


BASE_DIR = Path("/workspace/runpod-slim/ComfyUI")
WEB_DIR = Path("/workspace/webui")
LOG_FILE = Path("/workspace/runpod-slim/comfyui.log")

# Allowed model directories relative to the ComfyUI root
MODEL_DIRS: Dict[str, Path] = {
    "models/checkpoints": BASE_DIR / "models" / "checkpoints",
    "models/vae": BASE_DIR / "models" / "vae",
    "models/unet": BASE_DIR / "models" / "unet",
    "models/diffusion_models": BASE_DIR / "models" / "diffusion_models",
    "models/text_encoders": BASE_DIR / "models" / "text_encoders",
    "models/loras": BASE_DIR / "models" / "loras",
    "models/upscale_models": BASE_DIR / "models" / "upscale_models",
    "models/clip": BASE_DIR / "models" / "clip",
    "models/controlnet": BASE_DIR / "models" / "controlnet",
    "models/clip_vision": BASE_DIR / "models" / "clip_vision",
    "models/ipadapter": BASE_DIR / "models" / "ipadapter",
}

DOWNLOAD_TASKS: Dict[str, Dict[str, str]] = {}
TASK_LOCK = threading.Lock()


def safe_filename_from_url(url: str) -> str:
    parsed = urlparse(url)
    candidate = Path(parsed.path).name
    if not candidate:
        return f"download-{int(time.time())}"
    return candidate.split("?")[0] or f"download-{int(time.time())}"


def write_json(handler: SimpleHTTPRequestHandler, payload: dict, status: int = 200) -> None:
    data = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def read_request_json(handler: SimpleHTTPRequestHandler) -> dict:
    length = int(handler.headers.get("Content-Length", "0") or 0)
    raw = handler.rfile.read(length) if length > 0 else b"{}"
    try:
        return json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError:
        return {}


def list_custom_nodes() -> List[str]:
    nodes_dir = BASE_DIR / "custom_nodes"
    if not nodes_dir.exists():
        return []
    entries = []
    for item in sorted(nodes_dir.iterdir()):
        if item.is_dir() and not item.name.startswith("."):
            entries.append(item.name)
    return entries


def list_models() -> Dict[str, List[str]]:
    results: Dict[str, List[str]] = {}
    for key, path in MODEL_DIRS.items():
        if path.exists():
            files = [p.name for p in sorted(path.iterdir()) if p.is_file()]
            if files:
                results[key] = files
    return results


def tail_logs(limit: int = 400) -> str:
    if not LOG_FILE.exists():
        return ""
    try:
        with LOG_FILE.open("r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
        return "".join(lines[-limit:])
    except Exception:
        return ""


def download_file(url: str, dest: Path, headers: Optional[Dict[str, str]] = None) -> None:
    req = Request(url, headers=headers or {})
    with urlopen(req) as resp, dest.open("wb") as outfile:
        shutil.copyfileobj(resp, outfile)


def update_task(task_id: str, status: str, detail: Optional[str] = None) -> None:
    with TASK_LOCK:
        DOWNLOAD_TASKS[task_id] = {"status": status, "detail": detail or ""}


def start_download_task(source: str, url: str, model_type: str, api_key: Optional[str], filename: Optional[str]) -> str:
    task_id = f"task-{int(time.time()*1000)}"
    update_task(task_id, "downloading", "")

    def runner() -> None:
        headers: Dict[str, str] = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        dest_dir = MODEL_DIRS.get(model_type)
        if dest_dir is None:
            update_task(task_id, "failed", f"Unsupported model type: {model_type}")
            return

        dest_dir.mkdir(parents=True, exist_ok=True)

        target_name = filename or safe_filename_from_url(url)
        dest_path = (dest_dir / target_name).resolve()

        # Ensure destination remains inside the allowed directory
        if dest_dir.resolve() not in dest_path.parents and dest_dir.resolve() != dest_path:
            update_task(task_id, "failed", "Invalid destination path")
            return

        try:
            download_file(url, dest_path, headers=headers)
        except Exception as exc:  # noqa: BLE001 - surface download failures cleanly
            update_task(task_id, "failed", str(exc))
            return

        update_task(task_id, "success", str(dest_path))

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    return task_id


class WebUIHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_DIR), **kwargs)

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        # Keep logs concise; send to stdout
        print(f"[webui] {self.address_string()} - {format % args}")

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/api/status":
            payload = {
                "custom_nodes": list_custom_nodes(),
                "models": list_models(),
                "total_models": sum(len(v) for v in list_models().values()),
            }
            write_json(self, payload)
            return

        if parsed.path == "/logs":
            write_json(self, {"logs": tail_logs()})
            return

        if parsed.path == "/download/status":
            query = parse_qs(parsed.query)
            task_id = query.get("id", [""])[0]
            with TASK_LOCK:
                status = DOWNLOAD_TASKS.get(task_id)
            if not status:
                write_json(self, {"status": "unknown"}, status=HTTPStatus.NOT_FOUND)
                return
            write_json(self, status)
            return

        if parsed.path in {"/", "/index.html"}:
            self.path = "/web.html"
        return super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        if self.path.startswith("/download/"):
            payload = read_request_json(self)
            url = payload.get("url", "").strip()
            model_type = payload.get("model_type", "").strip()
            api_key = payload.get("api_key") or payload.get("token") or None
            filename = payload.get("filename") or None

            if not url:
                write_json(self, {"detail": "url is required"}, status=HTTPStatus.BAD_REQUEST)
                return

            if model_type not in MODEL_DIRS:
                write_json(self, {"detail": "Unsupported model_type"}, status=HTTPStatus.BAD_REQUEST)
                return

            task_id = start_download_task(self.path, url, model_type, api_key, filename)
            write_json(self, {"task_id": task_id}, status=HTTPStatus.ACCEPTED)
            return

        write_json(self, {"detail": "Unsupported path"}, status=HTTPStatus.NOT_FOUND)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Lightweight helper UI server")
    parser.add_argument("--port", type=int, default=8189)
    parser.add_argument("--host", type=str, default="0.0.0.0")
    parser.add_argument("--web-root", type=str, default=str(WEB_DIR))
    parser.add_argument("--comfy-root", type=str, default=str(BASE_DIR))
    parser.add_argument("--log-file", type=str, default=str(LOG_FILE))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    global BASE_DIR, WEB_DIR, LOG_FILE, MODEL_DIRS  # noqa: PLW0603
    BASE_DIR = Path(args.comfy_root)
    WEB_DIR = Path(args.web_root)
    LOG_FILE = Path(args.log_file)
    MODEL_DIRS = {
        key: BASE_DIR / Path(key)
        for key in [
            "models/checkpoints",
            "models/vae",
            "models/unet",
            "models/diffusion_models",
            "models/text_encoders",
            "models/loras",
            "models/upscale_models",
            "models/clip",
            "models/controlnet",
            "models/clip_vision",
            "models/ipadapter",
        ]
    }

    server = ThreadingHTTPServer((args.host, args.port), WebUIHandler)
    print(f"[webui] serving {WEB_DIR} on {args.host}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
