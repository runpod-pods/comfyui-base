"""ComfyUI end-to-end functional check (test_comfyui_functional manifest field).

Runs entirely HOST-SIDE against the pod's public Runpod proxy URL
(``https://<pod-id>-<COMFYUI_PORT>.proxy.runpod.net``) — no SSH, no in-pod
script. The comfyui-base image bakes in the ComfyUI-RunpodDirect custom node
(see the Dockerfile), which registers ``/server_download/*`` HTTP routes on
the SAME ComfyUI server as ``/prompt`` / ``/history`` / ``/view``. So we can
provision the checkpoint over HTTP exactly like the "Download to Pod" button
in the ComfyUI missing-models dialog does — everything is reachable through
the public proxy, which is why this test needs neither SSH nor filesystem
access to the pod.

Flow (see ``run_comfyui_check``):
  1. wait for ``/system_stats`` to answer through the proxy,
  2. for each model in tests/comfyui/models.json: confirm it's present (via
     ``/server_download/verify_model_integrity``), else queue a download with
     ``/server_download/start`` and poll ``/server_download/status`` to done,
  3. POST the workflow to ``/prompt``, poll ``/history/<id>``,
  4. GET ``/view`` and assert a real, non-empty PNG (optionally save a copy).

Kept separate from checks.py, which owns the generic/smoke probes (SSH, CUDA,
Jupyter, per-port reachability — including the ComfyUI ``:8188`` reachability
smoke that runs BEFORE this functional check).
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Optional

from . import config

_UA = "runpod-smoke-test/1.0"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _base_url(pod_id: str) -> str:
    return f"https://{pod_id}-{config.COMFYUI_PORT}.proxy.runpod.net"


def _get(url: str, timeout: int = 30) -> tuple[int, bytes]:
    """GET returning ``(status, body)``. 4xx/5xx come back as ``(code, body)``
    instead of raising, so callers can inspect the error payload. Transport
    errors (connection refused / DNS / timeout) still raise ``OSError``."""
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _post_json(url: str, payload: dict, timeout: int = 30) -> tuple[int, bytes]:
    """POST a JSON body, same ``(status, body)`` contract as ``_get``."""
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json", "User-Agent": _UA},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _load_json_file(path: str):
    with open(path, "rb") as f:
        return json.loads(f.read())


# ---------------------------------------------------------------------------
# Individual steps
# ---------------------------------------------------------------------------


def _wait_server(base: str, emit: Callable[[str], None]) -> bool:
    """Poll ``/system_stats`` through the proxy until it answers 200. The
    public proxy is eventually-consistent (a fresh pod takes ~10-30s to enter
    the routing table) and the image copies ~8 GB of baked ComfyUI into
    /workspace before binding :8188, so the budget is generous."""
    deadline = time.monotonic() + config.COMFYUI_WAIT_TIMEOUT
    n = 0
    last = ""
    while time.monotonic() < deadline:
        n += 1
        try:
            code, _ = _get(base + "/system_stats", timeout=10)
            if code == 200:
                emit(f"ComfyUI /system_stats OK after {n} probe(s)")
                return True
            last = f"HTTP {code}"
        except OSError as e:
            last = f"{type(e).__name__}: {e}"
        if n % 10 == 0:
            emit(f"  ...waiting for ComfyUI via proxy "
                 f"({config.COMFYUI_WAIT_TIMEOUT}s budget, last={last})")
        time.sleep(5)
    emit(f"FAIL: ComfyUI /system_stats not reachable via proxy in "
         f"{config.COMFYUI_WAIT_TIMEOUT}s (last: {last})")
    return False


def _runpoddirect_folder_paths(
    base: str, emit: Callable[[str], None],
) -> tuple[Optional[dict], str]:
    """Feature-detect ComfyUI-RunpodDirect: fetch its ``folder_paths`` map.
    Returns ``(map | None, last_error)`` — None means the routes never
    answered 200 within the retry window.

    Retries for up to COMFYUI_ROUTES_TIMEOUT rather than taking one shot:
    the Runpod proxy is eventually-consistent and its replicas can disagree
    — /system_stats may have answered through a replica that knows the pod
    while the next request lands on one that 404s/5xxes. A single-shot
    probe misclassified such transient proxy errors as "node not installed
    in this image" (an intermittent CI FAIL on images where the node is
    definitely baked in). A genuinely absent node costs one extra
    COMFYUI_ROUTES_TIMEOUT of polling, which is acceptable for the
    unambiguous verdict."""
    deadline = time.monotonic() + config.COMFYUI_ROUTES_TIMEOUT
    attempt = 0
    last = ""
    while True:
        attempt += 1
        try:
            code, body = _get(base + "/server_download/folder_paths", timeout=20)
            if code == 200:
                try:
                    return json.loads(body), ""
                except Exception:
                    last = "HTTP 200 with non-JSON body"
            else:
                snippet = (body or b"")[:120].decode("utf-8", "replace")
                last = f"HTTP {code}: {snippet}".strip()
        except OSError as e:
            last = f"{type(e).__name__}: {e}"
        if time.monotonic() >= deadline:
            emit(
                f"  /server_download/folder_paths never answered 200 in "
                f"{config.COMFYUI_ROUTES_TIMEOUT}s ({attempt} attempts, "
                f"last: {last})"
            )
            return None, last
        if attempt == 1:
            emit(
                f"  /server_download/folder_paths not answering yet ({last}) "
                f"— retrying for up to {config.COMFYUI_ROUTES_TIMEOUT}s"
            )
        time.sleep(3)


def _model_present(
    base: str, directory: str, filename: str, sha: str,
    emit: Callable[[str], None],
) -> bool:
    """Ask RunpodDirect whether the model already exists (and, when a sha256
    is known, whether it matches). Lets us skip the download on a warm pod
    AND avoids the HTTP 400 that ``/start`` returns for an existing file."""
    payload: dict = {"directory": directory, "filename": filename}
    if sha:
        payload["hash"] = sha
        payload["hash_type"] = "sha256"
    try:
        code, body = _post_json(
            base + "/server_download/verify_model_integrity", payload, timeout=180,
        )
    except OSError as e:
        emit(f"  warn: verify_model_integrity errored: {e}")
        return False
    if code != 200:
        return False
    try:
        d = json.loads(body)
    except Exception:
        return False
    # valid == exists AND (hash matches OR no hash was provided).
    return bool(d.get("exists")) and bool(d.get("valid"))


def _poll_download(
    base: str, directory: str, filename: str,
    emit: Callable[[str], None],
) -> bool:
    """Poll ``/server_download/status/<save_path>/<filename>`` until the
    download reaches ``completed`` (RunpodDirect verifies size + sha256
    server-side) or ``error``/``cancelled``."""
    status_url = (
        f"{base}/server_download/status/{directory}/"
        f"{urllib.parse.quote(filename)}"
    )
    deadline = time.monotonic() + config.COMFYUI_DOWNLOAD_TIMEOUT
    last_logged_pct = -10
    while time.monotonic() < deadline:
        try:
            code, body = _get(status_url, timeout=20)
        except OSError as e:
            emit(f"  warn: status poll errored: {e}")
            time.sleep(3)
            continue
        if code == 404:
            # Not registered yet (or already reaped) — retry briefly.
            time.sleep(2)
            continue
        try:
            d = json.loads(body)
        except Exception:
            time.sleep(2)
            continue
        status = d.get("status")
        pct = int(d.get("progress") or 0)
        if pct >= last_logged_pct + 10:
            last_logged_pct = pct
            emit(f"  download {filename}: {status} {pct}%")
        if status == "completed":
            emit(f"download complete: {directory}/{filename} "
                 f"(size_verified={d.get('size_verified')}, "
                 f"hash_verified={d.get('hash_verified')})")
            return True
        if status in ("error", "cancelled"):
            emit(f"FAIL: download {status}: "
                 f"{str(d.get('error') or '')[:300]}")
            return False
        time.sleep(3)
    emit(f"FAIL: download did not finish in {config.COMFYUI_DOWNLOAD_TIMEOUT}s")
    return False


def _ensure_model(
    base: str, m: dict, emit: Callable[[str], None],
) -> bool:
    """Make one model from models.json present on the pod, using RunpodDirect:
    verify -> (if missing) queue download -> poll to completion."""
    directory = m.get("directory", "checkpoints")
    filename = m["filename"]
    sha = (m.get("sha256") or "").lower()

    if _model_present(base, directory, filename, sha, emit):
        emit(f"model already present + verified: {directory}/{filename}")
        return True

    payload: dict = {"url": m["url"], "save_path": directory, "filename": filename}
    if sha:
        payload["hash"] = sha
        payload["hash_type"] = "sha256"
    try:
        code, body = _post_json(
            base + "/server_download/start", payload, timeout=60,
        )
    except OSError as e:
        emit(f"FAIL: /server_download/start errored: {e}")
        return False
    text = body.decode("utf-8", "replace")
    if code == 400 and "already exists" in text.lower():
        # Raced with another writer between verify and start — treat as present.
        emit(f"model already present (start said: {text[:160]})")
        return True
    if code != 200:
        emit(f"FAIL: /server_download/start returned HTTP {code}: {text[:300]}")
        return False
    emit(f"queued download {directory}/{filename} "
         f"(multi-connection via RunpodDirect)...")
    return _poll_download(base, directory, filename, emit)


def _needed_checkpoints(workflow: dict) -> set:
    out: set = set()
    for node in workflow.values():
        if isinstance(node, dict) and node.get("class_type") == "CheckpointLoaderSimple":
            ck = (node.get("inputs") or {}).get("ckpt_name")
            if ck:
                out.add(ck)
    return out


def _checkpoints_visible(
    base: str, names: set, emit: Callable[[str], None],
) -> bool:
    """Confirm ComfyUI lists the checkpoint(s) in CheckpointLoaderSimple.
    A freshly-downloaded file may not be in the (cached) enum until ComfyUI
    rescans the dir, so callers retry this briefly."""
    try:
        code, body = _get(base + "/object_info/CheckpointLoaderSimple", timeout=30)
        info = json.loads(body)
        enum = info["CheckpointLoaderSimple"]["input"]["required"]["ckpt_name"][0]
    except Exception as e:
        emit(f"  warn: could not read /object_info: {e!r}")
        return False
    missing = [n for n in names if n not in enum]
    if missing:
        emit(f"  checkpoints not yet visible to ComfyUI: {missing}")
        return False
    return True


def _queue_prompt(
    base: str, workflow: dict, emit: Callable[[str], None],
) -> Optional[str]:
    code, body = _post_json(
        base + "/prompt",
        {"prompt": workflow, "client_id": "runpod-smoke"},
        timeout=60,
    )
    text = body.decode("utf-8", "replace")
    if code != 200:
        emit(f"FAIL: POST /prompt returned HTTP {code}: {text[:1000]}")
        return None
    try:
        resp = json.loads(body)
    except Exception:
        emit(f"FAIL: /prompt returned non-JSON: {text[:300]}")
        return None
    node_errors = resp.get("node_errors") or {}
    if node_errors:
        emit(f"FAIL: /prompt reported node_errors: "
             f"{json.dumps(node_errors)[:1000]}")
        return None
    return resp.get("prompt_id")


def _wait_result(
    base: str, prompt_id: str, emit: Callable[[str], None],
) -> Optional[list]:
    deadline = time.monotonic() + config.COMFYUI_GEN_TIMEOUT
    while time.monotonic() < deadline:
        try:
            code, body = _get(base + f"/history/{prompt_id}", timeout=20)
            entry = json.loads(body).get(prompt_id) if code == 200 else None
        except Exception:
            entry = None
        if entry:
            status = entry.get("status", {}) or {}
            if status.get("status_str") == "error":
                emit(f"FAIL: execution error: {json.dumps(status)[:1000]}")
                return None
            imgs = []
            for node_out in (entry.get("outputs") or {}).values():
                for im in (node_out.get("images") or []):
                    imgs.append(im)
            if imgs:
                return imgs
            if status.get("completed") is True:
                emit("FAIL: prompt completed but produced no image outputs")
                return None
        time.sleep(3)
    emit(f"FAIL: generation did not finish in {config.COMFYUI_GEN_TIMEOUT}s")
    return None


def _save_png(
    save_dir: str, tag: str, filename: str, data: bytes,
    emit: Callable[[str], None],
) -> None:
    """Write the fetched PNG under ``save_dir`` as ``<tag>_<filename>``.
    Best-effort: a save failure must not turn a passing check into a FAIL."""
    try:
        os.makedirs(save_dir, exist_ok=True)
        safe = os.path.basename(filename) or "output.png"
        prefix = re.sub(r"[^A-Za-z0-9._-]", "_", tag) if tag else ""
        out = os.path.join(save_dir, f"{prefix}_{safe}" if prefix else safe)
        with open(out, "wb") as f:
            f.write(data)
        emit(f"saved output PNG -> {os.path.abspath(out)} ({len(data)} bytes)")
    except Exception as e:  # pragma: no cover — defensive
        emit(f"warn: could not save output PNG: {e}")


def _fetch_and_validate(
    base: str, im: dict, save_dir: str, tag: str,
    emit: Callable[[str], None],
) -> bool:
    q = urllib.parse.urlencode({
        "filename": im.get("filename", ""),
        "subfolder": im.get("subfolder", ""),
        "type": im.get("type", "output"),
    })
    try:
        code, data = _get(base + "/view?" + q, timeout=60)
    except OSError as e:
        emit(f"FAIL: /view request errored: {e!r}")
        return False
    if code != 200 or not data:
        emit(f"FAIL: /view returned HTTP {code}, {len(data or b'')} bytes")
        return False
    if data[:8] != _PNG_MAGIC:
        emit(f"FAIL: output is not a PNG (first bytes: {data[:8]!r})")
        return False
    if len(data) < 1000:
        emit(f"FAIL: PNG suspiciously small ({len(data)} bytes)")
        return False
    try:
        w = int.from_bytes(data[16:20], "big")
        h = int.from_bytes(data[20:24], "big")
    except Exception:
        w = h = 0
    emit(f"OK: got PNG {im.get('filename')} ({len(data)} bytes, {w}x{h})")
    if not (w > 0 and h > 0):
        return False
    if save_dir:
        _save_png(save_dir, tag, im.get("filename", "output.png"), data, emit)
    return True


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run_comfyui_check(
    pod_id: str,
    on_line: Optional[Callable[[str], None]] = None,
    save_dir: str = "",
    tag: str = "",
) -> tuple[bool, str]:
    """Run the ComfyUI end-to-end functional probe against the pod's public
    proxy URL (``https://<pod-id>-<COMFYUI_PORT>.proxy.runpod.net``):

      wait for the server -> provision the model(s) from
      tests/comfyui/models.json via ComfyUI-RunpodDirect's
      ``/server_download/*`` routes -> POST the workflow to ``/prompt`` ->
      poll ``/history`` -> fetch the result via ``/view`` and validate it's a
      real, non-empty PNG.

    Progress is streamed line-by-line via ``on_line`` (model download +
    generation take minutes; without streaming the run looks frozen). When
    ``save_dir`` is set (config.COMFYUI_SAVE_DIR) the fetched PNG is written
    under it as ``<tag>_<filename>`` (``tag`` is the pod id, to disambiguate
    parallel runs); the pod is torn down right after this check, so that's the
    only chance to keep a copy.

    Returns ``(ok, reason)`` — ``reason`` is a short human string used for the
    FAIL outcome. No wall-clock watchdog is needed (unlike the old SSH probe):
    every poll loop is bounded by a monotonic deadline and requests carry
    their own socket timeout, so this can't hang or leak a pod.
    """
    def emit(msg: str) -> None:
        if on_line is not None:
            on_line(msg)

    base = _base_url(pod_id)
    emit(f"ComfyUI functional check via proxy: {base}")

    if not _wait_server(base, emit):
        return False, "ComfyUI /system_stats not reachable via proxy"

    try:
        workflow = _load_json_file(config.COMFYUI_WORKFLOW)
        models = _load_json_file(config.COMFYUI_MODELS_MANIFEST)
    except OSError as e:
        return False, f"could not read ComfyUI test assets: {e}"

    if models:
        folder_paths, routes_err = _runpoddirect_folder_paths(base, emit)
        if folder_paths is None:
            return False, (
                "ComfyUI-RunpodDirect routes (/server_download/*) never "
                f"answered within {config.COMFYUI_ROUTES_TIMEOUT}s "
                f"(last: {routes_err}) — node missing from the image, or "
                "the proxy kept failing; cannot provision the model over HTTP"
            )
        for m in models:
            if not _ensure_model(base, m, emit):
                return False, f"model provisioning failed for {m.get('filename')}"

    needed = _needed_checkpoints(workflow)
    if needed:
        visible = False
        for _ in range(15):
            if _checkpoints_visible(base, needed, emit):
                visible = True
                break
            time.sleep(2)
        if not visible:
            return False, (
                f"ComfyUI does not list required checkpoint(s) {sorted(needed)} "
                "after download"
            )

    pid = _queue_prompt(base, workflow, emit)
    if not pid:
        return False, "workflow was rejected by /prompt"
    emit(f"queued prompt_id={pid}, waiting up to {config.COMFYUI_GEN_TIMEOUT}s "
         "for the image...")

    imgs = _wait_result(base, pid, emit)
    if not imgs:
        return False, "generation produced no image / errored"

    if not _fetch_and_validate(base, imgs[0], save_dir, tag, emit):
        return False, "output PNG failed validation"

    emit("COMFYUI FUNCTIONAL CHECK OK")
    return True, "generated + validated PNG"
