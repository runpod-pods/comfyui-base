"""SSH-based health and functional checks.

  * ssh_probe — one-shot connection probe, used as the real readiness signal
  * cuda_check_command / run_cuda_check — torch.cuda or nvidia-smi assertion
  * jupyter_check_command / run_jupyter_check — in-pod Jupyter probe over SSH
  * run_jupyter_proxy_check — public proxy probe from the test machine
  * fetch_logs_via_ssh / dump_pod_logs — pull diagnostic info before terminating

Selection of the CUDA check is driven by the IMAGE REF, not the manifest
group name: new groups added in the future won't silently skip the check.
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from typing import Callable, Optional

from . import config
from .log import log
from .runpodctl import runpodctl_json


# Error string returned by every helper that shells out to `ssh` and fails
# because the binary isn't on $PATH. Centralised so the message stays
# identical across helpers (callers grep for it in logs).
_SSH_BINARY_NOT_FOUND = "ssh binary not found"


# ---------------------------------------------------------------------------
# SSH plumbing
# ---------------------------------------------------------------------------


def _resolve_ssh_identity() -> Optional[str]:
    """Find the SSH private key to use for the runpodctl-managed PUBLIC_KEY.

    Order of preference:
      1. RUNPOD_SSH_KEY env var (explicit override)
      2. runpodctl-managed key at ~/.runpod/ssh/runpodctl-ssh-key
      3. Standard ssh defaults (~/.ssh/id_ed25519, ~/.ssh/id_rsa, ssh-agent)
    Returns the path if a non-default key was found, else None (let ssh
    pick a default from its standard search path / ssh-agent)."""
    if config.SSH_IDENTITY:
        return config.SSH_IDENTITY if os.path.isfile(config.SSH_IDENTITY) else None
    for candidate in (
        "~/.runpod/ssh/runpodctl-ssh-key",
        "~/.runpod/runpodctl-ssh-key",
        "~/.ssh/runpodctl-ssh-key",
    ):
        path = os.path.expanduser(candidate)
        if os.path.isfile(path):
            return path
    return None


def _ssh_command_prefix(host: str, port: int) -> list[str]:
    """Build the `ssh ... root@<host> -p <port>` prefix common to all SSH calls."""
    cmd = ["ssh", *config.SSH_OPTS, "-p", str(port)]
    identity = _resolve_ssh_identity()
    if identity:
        cmd.extend(["-i", identity])
    cmd.append(f"root@{host}")
    return cmd


def ssh_probe(host: str, port: int, timeout: int = 8) -> tuple[bool, str]:
    """One-shot SSH connection attempt. Returns (success, stderr_excerpt).
    Used as the real container-readiness signal."""
    cmd = [*_ssh_command_prefix(host, port), "echo", "ready"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "ssh probe timed out"
    except FileNotFoundError:
        return False, _SSH_BINARY_NOT_FOUND
    if r.returncode == 0 and "ready" in r.stdout:
        return True, ""
    return False, (r.stderr or r.stdout).strip()[:200]


# ---------------------------------------------------------------------------
# CUDA / GPU functional check
# ---------------------------------------------------------------------------

# Image-tag substrings/patterns we treat as "this image expects a GPU
# runtime" (NVIDIA CUDA or AMD ROCm). Kept loose on purpose — the worst
# case for a false positive is running nvidia-smi on a CPU pod, which just
# returns non-zero and surfaces as a FAIL we'd want to see anyway.
_GPU_TAG_RE = re.compile(
    # 'cuda1281', 'cuda1300', or the bare word 'cuda' (e.g. nvidia/cuda:...)
    r"\bcuda\b|\bcuda\d"
    # 'cu1281', 'cu1290' short form. Boundary prevents matching 'cube',
    # 'cute', etc. — we require a digit immediately after 'cu'.
    r"|(?:^|[^a-z0-9])cu\d"
    # AMD ROCm tag fragments: 'rocm', 'rocm644'.
    r"|\brocm",
    re.IGNORECASE,
)

# Image-name/tag markers that imply PyTorch is installed in the *system*
# Python (i.e. `python -c "import torch"` will work over SSH).
# Deliberately does NOT match autoresearch: its torch lives in
# /opt/autoresearch/.venv (uv-managed) and isn't on sys.path for the
# system interpreter we ssh into.
_TORCH_TAG_RE = re.compile(
    # 'pytorch' anywhere in name or tag covers runpod/pytorch,
    # runpod/nvidia-pytorch, and base images with -pytorch251-style tags.
    r"\bpytorch"
    # Tag fragments like 'torch260', 'torch271' — short form some images use.
    r"|(?:^|[^a-z0-9])torch\d",
    re.IGNORECASE,
)

# Image-tag markers for AMD ROCm runtimes. Matched BEFORE the torch regex
# so that ROCm-pytorch images (which inherit from rocm/pytorch:* and keep
# torch in a conda env not visible to the system `python`) take the
# rocm-smi path instead of falling into the import-torch path and failing
# with a misleading "ModuleNotFoundError".
_ROCM_TAG_RE = re.compile(r"\brocm", re.IGNORECASE)


def _image_expects_gpu(image: str) -> bool:
    """True if the image ref implies a GPU runtime (CUDA or ROCm) inside."""
    return bool(_GPU_TAG_RE.search(image))


def _image_expects_torch(image: str) -> bool:
    """True if the image ref implies PyTorch is importable from system Python."""
    return bool(_TORCH_TAG_RE.search(image))


def _image_expects_rocm(image: str) -> bool:
    """True if the image ref implies an AMD ROCm runtime."""
    return bool(_ROCM_TAG_RE.search(image))


def cuda_check_command(image: str) -> str:
    """Return a shell command that functionally validates the GPU/CUDA stack
    for a given image, or '' to skip the check (CPU images).

    Selection is driven by the IMAGE REF (not the manifest group name) so
    new manifest groups added in the future won't silently skip the check.

    Logic (first match wins):
        - has 'rocm' in ref                          -> run rocm-smi check
          (AMD GPUs; runpod/base ROCm-pytorch images inherit from
          rocm/pytorch:* where torch lives in a conda env not visible to
          the system `python`, so the torch.cuda path would falsely fail
          with ModuleNotFoundError)
        - has 'pytorch' / 'torch\\d' in ref          -> run torch.cuda check
          (covers runpod/pytorch, runpod/nvidia-pytorch — NVIDIA stack)
        - has 'cuda' / 'cu\\d' only                  -> run nvidia-smi check
          (runpod/base GPU tags and autoresearch — torch in venv not
          visible to system python)
        - none of the above                          -> CPU image, no check

    The returned command MUST exit non-zero on failure so the SSH call can
    detect it. Output is captured for the run report.
    """
    if _image_expects_rocm(image):
        # AMD ROCm path. `rocm-smi` is the AMD counterpart to nvidia-smi
        # and ships in every official rocm/* base image. We assert it
        # finds at least one GPU by grepping for the GPU table header.
        return (
            "set -e; "
            "rocm-smi --showproductname --showmeminfo vram; "
            "rocm-smi --showid | grep -qE '^GPU\\[[0-9]+\\]' "
            "  || { echo 'FAIL: rocm-smi reported no GPUs'; exit 1; }; "
            "if command -v hipcc >/dev/null; then "
            "  hipcc --version | head -n 2; "
            "else "
            "  echo 'hipcc not in PATH (HIP toolkit may be runtime-only)'; "
            "fi"
        )

    if _image_expects_torch(image):
        # Use `python` (the runpod/base symlink /usr/local/bin/python ->
        # /usr/bin/python3.12), NOT `python3`. On Ubuntu 22.04 system
        # `python3` resolves to python3.10 — but pytorch/Dockerfile
        # installs torch via `python -m pip`, so torch only exists in
        # python3.12's site-packages. On 24.04 they happen to coincide.
        # Using `python` is portable.
        return (
            "python - <<'PY'\n"
            "import sys, torch\n"
            "assert torch.cuda.is_available(), 'torch.cuda.is_available() returned False'\n"
            "n = torch.cuda.device_count()\n"
            "assert n > 0, 'torch.cuda.device_count() == 0'\n"
            "name = torch.cuda.get_device_name(0)\n"
            "cap = torch.cuda.get_device_capability(0)\n"
            "print(f'torch={torch.__version__} cuda={torch.version.cuda} '\n"
            "      f'gpus={n} dev0={name!r} compute={cap[0]}.{cap[1]}')\n"
            "# Tiny tensor-on-GPU sanity check: catches driver bugs that pass\n"
            "# is_available() but fail on actual memory ops.\n"
            "x = torch.ones(64, 64, device='cuda')\n"
            "y = (x @ x).sum().item()\n"
            "assert y == 64*64*64, f'matmul gave {y}, expected {64*64*64}'\n"
            "print('matmul ok')\n"
            "PY"
        )

    if _image_expects_gpu(image):
        # GPU image without system-Python torch (raw base, autoresearch's
        # uv-venv'd torch, etc.). Verify the toolkit + driver respond to a
        # real query — more than just an nvidia-smi banner.
        return (
            "set -e; "
            "nvidia-smi -L; "
            "nvidia-smi --query-gpu=name,driver_version,memory.total "
            "  --format=csv,noheader; "
            "if command -v nvcc >/dev/null; then "
            "  nvcc --version | tail -n 2; "
            "else "
            "  echo 'nvcc not in PATH (CUDA toolkit may be runtime-only)'; "
            "fi"
        )

    # No GPU/torch markers — treat as CPU image. Boot + dwell is the only
    # gate; no extra functional check to run.
    return ""


def run_cuda_check(host: str, port: int, image: str) -> tuple[bool, str]:
    """Run the GPU/CUDA functional check inside the pod over SSH.
    Returns (ok, output). ok=True when:
      * the image has no GPU check defined (treated as pass), OR
      * the remote command exits 0.
    output contains stdout+stderr for inclusion in the run log."""
    cmd = cuda_check_command(image)
    if not cmd:
        return True, "(no GPU check for this image)"
    ssh_cmd = [*_ssh_command_prefix(host, port), cmd]
    try:
        r = subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return False, "cuda check timed out after 60s"
    except FileNotFoundError:
        return False, _SSH_BINARY_NOT_FOUND
    combined = (r.stdout + r.stderr).strip()
    return (r.returncode == 0), combined


# ---------------------------------------------------------------------------
# Jupyter checks (opt-in)
# ---------------------------------------------------------------------------


def jupyter_check_command(timeout: int) -> str:
    """Shell snippet (run via SSH on the pod) that verifies Jupyter Lab is
    actually running and answers HTTP with the token we set via env.

    Why both `jupyter server list` AND a `curl`: the list catches the silent
    `python3 -m jupyter` failure mode (server never started — list is empty
    even though start.sh printed 'Jupyter Lab started'); the curl catches
    "process is alive but http endpoint is wedged" or "token mismatch".

    Polls for up to `timeout` seconds because start.sh launches Jupyter in
    background via `nohup ... &` and exits without waiting; the HTTP port
    typically becomes reachable a few seconds after the pod logs say it is.
    """
    return (
        "set -e; "
        # Wait for the HTTP port to open. Don't rely on `jupyter` CLI being
        # in PATH yet (the binary IS in PATH from the base image, but the
        # server takes a few seconds to bind). Use raw /dev/tcp instead so
        # we don't need nc / curl just to detect "listening".
        f"for i in $(seq 1 {timeout}); do "
        "  if (echo > /dev/tcp/127.0.0.1/8888) 2>/dev/null; then break; fi; "
        "  sleep 1; "
        "done; "
        # Server should appear in `jupyter server list`. If start.sh used
        # the wrong python interpreter, this is empty.
        "echo '--- jupyter server list ---'; "
        "OUT=$(jupyter server list 2>&1 || true); "
        "echo \"$OUT\"; "
        "echo \"$OUT\" | grep -qE 'http://[^ ]*:8888' "
        "  || { echo 'FAIL: no Jupyter server listening on :8888'; exit 1; }; "
        # API responds with our token. /api/status is a tiny endpoint that
        # returns 200 + JSON when the server is healthy AND auth passes.
        "echo '--- curl /api/status ---'; "
        f"curl -sS --max-time 10 -o /tmp/_jupyter_status "
        f"  -w 'http=%{{http_code}}\\n' "
        f"  \"http://127.0.0.1:8888/api/status?token={config.JUPYTER_TEST_PASSWORD}\" "
        "  || { echo 'FAIL: curl to :8888 failed'; exit 1; }; "
        "cat /tmp/_jupyter_status; echo; "
        "grep -qE '^http=200' /tmp/_jupyter_status 2>/dev/null "
        "  || grep -qE '\"started\"' /tmp/_jupyter_status "
        "  || { echo 'FAIL: /api/status did not return 200 with valid token'; "
        "       exit 1; }; "
        "echo 'jupyter check OK'"
    )


def run_jupyter_check(host: str, port: int) -> tuple[bool, str]:
    """SSH into the pod and run the jupyter probe against 127.0.0.1:8888.

    This validates the IN-POD side: start.sh launched Jupyter with the
    right interpreter, server bound to :8888, our token works.

    Returns (ok, output). ok=False when the SSH call itself failed, OR
    when jupyter probe exited non-zero (server not running / wrong token
    / API not healthy)."""
    cmd = jupyter_check_command(config.JUPYTER_WAIT_TIMEOUT)
    ssh_cmd = [*_ssh_command_prefix(host, port), cmd]
    # SSH command has its own grace loop (JUPYTER_WAIT_TIMEOUT) plus a 10s
    # curl; pad the outer timeout to leave room for SSH handshake.
    outer_timeout = config.JUPYTER_WAIT_TIMEOUT + 30
    try:
        r = subprocess.run(
            ssh_cmd, capture_output=True, text=True, timeout=outer_timeout
        )
    except subprocess.TimeoutExpired:
        return False, f"jupyter check timed out after {outer_timeout}s"
    except FileNotFoundError:
        return False, _SSH_BINARY_NOT_FOUND
    combined = (r.stdout + r.stderr).strip()
    return (r.returncode == 0), combined


# ---------------------------------------------------------------------------
# Generic per-port checks (test_ports manifest field)
# ---------------------------------------------------------------------------


def port_check_command(test_port: int, wait_timeout: int) -> str:
    """Shell snippet (run via SSH) that verifies *some* server binds to
    127.0.0.1:<test_port> inside the pod and responds to a plain HTTP GET.
    Generic counterpart to `jupyter_check_command` — no app-specific
    assertions, just "a server is listening and the response is < 500".
    Treats 4xx as OK (the server IS responding — auth/redirect/etc. is
    expected for many apps when GETting /).

    Implementation note: unifies "port bound" + "HTTP <500" detection
    into a SINGLE retry loop. Earlier we polled the TCP bind for up to
    `wait_timeout` seconds and then took ONE curl shot with --max-time 10
    — that broke on ComfyUI, which binds 8188 within ~3s but then keeps
    its HTTP server unresponsive for ~90s while ComfyUI-Manager fetches
    ComfyRegistry data on first boot. The single curl shot was a coin
    toss depending on where in that window we landed. The unified loop
    keeps retrying both the bind probe and the HTTP shot until something
    answers with <500 — fast apps still exit in <1s, slow apps like
    ComfyUI get the full `wait_timeout` budget for HTTP readiness.
    """
    return (
        "set -e; "
        f"echo 'Probing 127.0.0.1:{test_port} (timeout {wait_timeout}s)...'; "
        # Sentinel: 'pending' means no HTTP attempt succeeded yet. After
        # the loop we case-match the final value so a flake right at the
        # last iteration still fails clean.
        "CODE=pending; "
        f"for i in $(seq 1 {wait_timeout}); do "
        # 1. Cheap bind probe via /dev/tcp. If the port isn't open yet
        #    we don't waste a curl invocation on it.
        f"  if (echo > /dev/tcp/127.0.0.1/{test_port}) 2>/dev/null; then "
        # 2. Port is open — try a short curl. --max-time 5 keeps each
        #    iteration responsive; the outer loop is what gives us
        #    patience across long warm-up windows.
        f"    CODE=$(curl -sS --max-time 5 -o /dev/null "
        f"      -w '%{{http_code}}' 'http://127.0.0.1:{test_port}/' "
        "      || echo 'curl_failed'); "
        "    case \"$CODE\" in "
        # 1xx/2xx/3xx/4xx — server is responding. Done.
        "      [1234]*) "
        f"        echo \"port {test_port} responsive after \"$i\"s: http=$CODE\"; "
        "        break ;; "
        # 000 (transport failure), curl_failed, 5xx — keep retrying.
        "      *) ;; "
        "    esac; "
        "  fi; "
        # Heartbeat every 30s so the operator on the other side of the
        # SSH pipe sees the probe is still alive (and what the last HTTP
        # code looked like) instead of staring at silence for up to
        # PORT_WAIT_TIMEOUT seconds.
        "  if [ $((i % 30)) -eq 0 ]; then "
        f"    echo \"  ...still probing 127.0.0.1:{test_port} at ${{i}}s/{wait_timeout}s (last code=$CODE)\"; "
        "  fi; "
        "  sleep 1; "
        "done; "
        # Final classification. CODE is 'pending' if the loop never even
        # saw a bound port; otherwise it's the last HTTP code seen.
        "case \"$CODE\" in "
        "  [1234]*) "
        f"    echo 'port {test_port} OK' ;; "
        "  pending) "
        f"    echo \"FAIL: nothing ever listened on 127.0.0.1:{test_port}\" "
        f"      ' within {wait_timeout}s'; "
        "    exit 1 ;; "
        "  *) "
        f"    echo \"FAIL: port {test_port} never returned an HTTP <500 \" "
        f"      'response within {wait_timeout}s (last code: '$CODE')'; "
        "    exit 1 ;; "
        "esac"
    )


def run_port_check(
    host: str, port: int, test_port: int,
    on_line: Optional[Callable[[str], None]] = None,
) -> tuple[bool, str]:
    """SSH into the pod and probe `127.0.0.1:<test_port>` for an HTTP
    response. ok=True iff the server bound the port AND returned an
    HTTP <500.

    Streams stdout line-by-line via the optional `on_line` callback so
    the operator sees live progress (the bash script emits heartbeat
    lines every 30s). With PORT_WAIT_TIMEOUT now at 900s this matters:
    earlier we used subprocess.run(capture_output=True), which silently
    buffers the entire output until the SSH process exits — meaning
    15 minutes of nothing followed by a wall of text, including for
    plain successes.

    Returns (ok, last_line) — `last_line` is the final stdout line
    (typically the bash 'port N OK' / 'FAIL: ...' summary), kept as a
    short reason string for the FAIL outcome of the runner.
    """
    cmd = port_check_command(test_port, config.PORT_WAIT_TIMEOUT)
    ssh_cmd = [*_ssh_command_prefix(host, port), cmd]
    # Outer wall-clock budget: the bash loop is bounded by wait_timeout
    # internally, +60s buffer covers SSH handshake and any tail we tack
    # on (diagnostic block, etc.).
    outer_timeout = config.PORT_WAIT_TIMEOUT + 60
    try:
        proc = subprocess.Popen(
            ssh_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,  # line-buffered on the Python side
        )
    except FileNotFoundError:
        return False, _SSH_BINARY_NOT_FOUND
    assert proc.stdout is not None

    # Hard wall-clock watchdog. Without this, `proc.stdout.readline()`
    # blocks forever if the SSH transport is still up but the remote
    # bash has hung (or the connection is silently half-closed). One
    # such hang during an overnight run kept multiple pods alive for
    # 12 hours and racked up real charges — never again.
    timed_out_via_watchdog = [False]

    def _kill_on_timeout() -> None:
        timed_out_via_watchdog[0] = True
        try:
            proc.kill()
        except Exception:
            pass

    watchdog = threading.Timer(outer_timeout, _kill_on_timeout)
    watchdog.daemon = True
    watchdog.start()

    last_line = ""
    try:
        for raw in iter(proc.stdout.readline, ""):
            line = raw.rstrip("\n")
            if line:
                if on_line is not None:
                    on_line(line)
                last_line = line
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    except Exception as e:  # pragma: no cover — defensive
        try:
            proc.kill()
        except Exception:
            pass
        return False, f"port {test_port} check errored: {e}"
    finally:
        watchdog.cancel()

    if timed_out_via_watchdog[0]:
        return (
            False,
            f"port {test_port} check wall-clock timeout after {outer_timeout}s "
            "(remote SSH/bash hung)",
        )
    return (proc.returncode == 0), last_line


def run_port_proxy_check(
    pod_id: str, test_port: int,
) -> tuple[bool, str]:
    """Hit `https://<pod-id>-<test_port>.proxy.runpod.net/` from the test
    machine. Verifies the Runpod public proxy registered the port (only
    `<port>/http` declarations get registered) AND the server actually
    answers end-to-end.

    Like `port_check_command`, accepts 2xx-4xx as success — many apps
    return 401/403 on / when no auth header is provided, which still
    means "server is up and proxied". Only 5xx and transport errors
    count as failure.
    """
    url = f"https://{pod_id}-{test_port}.proxy.runpod.net/"
    deadline = time.monotonic() + config.PORT_PROXY_TIMEOUT
    lines = [f"GET {url}"]
    last_err = ""
    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "runpod-smoke-test/1.0"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                code = resp.status
                # Read enough to see the server header / first body bytes
                # but don't drown the log if it's HTML.
                body = resp.read(256).decode("utf-8", errors="replace")
                lines.append(
                    f"attempt #{attempt}: HTTP {code} body={body[:160]!r}"
                )
                if code < 500:
                    return True, "\n".join(lines)
                last_err = f"HTTP {code}"
        except urllib.error.HTTPError as e:
            # 4xx is raised as HTTPError by urlopen; treat them as success
            # (server responded, just unauthenticated/redirected). Only
            # 5xx and the connection-level errors below count as failure.
            if e.code < 500:
                lines.append(
                    f"attempt #{attempt}: HTTP {e.code} {e.reason} "
                    "(server responding)"
                )
                return True, "\n".join(lines)
            last_err = f"HTTP {e.code} {e.reason}"
            lines.append(f"attempt #{attempt}: {last_err}")
        except OSError as exc:
            # urllib.error.URLError + builtin TimeoutError both derive
            # from OSError — covers connection-refused, DNS, timeouts.
            last_err = f"{type(exc).__name__}: {exc}"
            lines.append(f"attempt #{attempt}: {last_err}")
        time.sleep(5)

    lines.append(
        f"FAIL: proxy unreachable after {config.PORT_PROXY_TIMEOUT}s "
        f"({attempt} attempts), last error: {last_err}"
    )
    return False, "\n".join(lines)


def run_jupyter_proxy_check(pod_id: str) -> tuple[bool, str]:
    """Hit `https://<pod-id>-8888.proxy.runpod.net/api/status?token=admin`
    from the test machine. Verifies that:

      1. Runpod's public proxy has the pod registered for port 8888.
         If the port was exposed as `8888/tcp` instead of `8888/http`, the
         proxy never wires it up and this fails. The SSH-side check would
         still pass — that's exactly the kind of misconfiguration the
         end-user would hit when they tried to open Jupyter from the UI.
      2. Jupyter is reachable end-to-end, not just on localhost.

    Retries for up to JUPYTER_PROXY_TIMEOUT seconds because the proxy is
    eventually-consistent: a freshly-created pod may not be in its routing
    table for ~10–30s. Returns (ok, multi-line log).
    """
    url = (
        f"https://{pod_id}-8888.proxy.runpod.net/api/status"
        f"?token={config.JUPYTER_TEST_PASSWORD}"
    )
    # Same URL with the token stripped — used in log lines so we never
    # write the bearer token to stdout / CI logs. The real `url` (with
    # token) only ever goes to urlopen.
    redacted_url = (
        f"https://{pod_id}-8888.proxy.runpod.net/api/status?token=<redacted>"
    )
    deadline = time.monotonic() + config.JUPYTER_PROXY_TIMEOUT
    lines = [f"GET {redacted_url}"]
    last_err = ""
    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "runpod-smoke-test/1.0"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                code = resp.status
                body = resp.read(2048).decode("utf-8", errors="replace")
                lines.append(
                    f"attempt #{attempt}: HTTP {code} body={body[:200]}"
                )
                if code == 200:
                    return True, "\n".join(lines)
                last_err = f"HTTP {code}"
        except urllib.error.HTTPError as e:
            last_err = f"HTTP {e.code} {e.reason}"
            lines.append(f"attempt #{attempt}: {last_err}")
        except OSError as exc:
            # urllib.error.URLError and the builtin TimeoutError both derive
            # from OSError, so this single clause covers connection refused,
            # DNS failures, socket timeouts, and 'No route to host' alike.
            last_err = f"{type(exc).__name__}: {exc}"
            lines.append(f"attempt #{attempt}: {last_err}")
        time.sleep(5)

    lines.append(
        f"FAIL: proxy unreachable after {config.JUPYTER_PROXY_TIMEOUT}s "
        f"({attempt} attempts), last error: {last_err}"
    )
    return False, "\n".join(lines)


# ---------------------------------------------------------------------------
# Diagnostic log fetch
# ---------------------------------------------------------------------------


def _gpu_smi_block(image: str) -> str:
    """Pick the right vendor SMI for the diagnostic dump. Avoids the
    'bash: line 1: rocm-smi: command not found' noise on NVIDIA pods
    (and the inverse on AMD pods). Falls back to skipping the SMI section
    entirely for CPU images.

    Mirrors the same heuristic as `cuda_check_command`'s functional
    check — _image_expects_rocm wins first so ROCm-pytorch images don't
    fall into the nvidia-smi path.
    """
    if _image_expects_rocm(image):
        return (
            "echo '=== rocm-smi ==='; "
            "if command -v rocm-smi >/dev/null 2>&1; then "
            "  rocm-smi 2>&1 | head -n 25; "
            "else "
            "  echo '(rocm-smi not in PATH)'; "
            "fi; "
        )
    if _image_expects_gpu(image):
        return (
            "echo '=== nvidia-smi ==='; "
            "if command -v nvidia-smi >/dev/null 2>&1; then "
            "  nvidia-smi 2>&1 | head -n 15; "
            "else "
            "  echo '(nvidia-smi not in PATH)'; "
            "fi; "
        )
    # CPU image — no GPU SMI section, just keep the dump shorter.
    return ""


def _runtime_state_block(tail: int) -> str:
    """Shell snippet that dumps in-container runtime state useful for
    diagnosing port-check failures:

      * top processes (so we can see if main.py is still running, stuck in
        cp -r, or already gone)
      * listening TCP ports (so we can distinguish "ComfyUI never bound" from
        "ComfyUI bound but firewalled/CORS-wedged"; ss prints the owning pid
        so we link a port back to a process)
      * size of /workspace/runpod-slim/ComfyUI (the first-boot cp -r of
        ~8 GB is the biggest single warmup cost — knowing it's still growing
        vs already done resolves "is it stuck or just slow" instantly)
      * tails of side-server logs (start.sh redirects jupyter/filebrowser
        stdout there; ComfyUI's own stdout goes to PID 1 stdout which we
        can't read from another process, hence no comfy.log tail — see ps).
    """
    return (
        "echo '=== ps (top 25 by RSS) ==='; "
        "ps -eo pid,ppid,stat,rss,etime,cmd --sort=-rss --no-headers 2>/dev/null "
        "  | head -n 25 || ps aux 2>&1 | head -n 25; "
        "echo '=== listening TCP ports ==='; "
        "if command -v ss >/dev/null 2>&1; then "
        "  ss -tlnp 2>&1 | head -n 30; "
        "elif command -v netstat >/dev/null 2>&1; then "
        "  netstat -tlnp 2>&1 | head -n 30; "
        "else "
        "  echo '(neither ss nor netstat available)'; "
        "fi; "
        "echo '=== ComfyUI workspace state ==='; "
        "if [ -d /workspace/runpod-slim/ComfyUI ]; then "
        "  du -sh /workspace/runpod-slim/ComfyUI 2>/dev/null "
        "    || echo '(du failed)'; "
        "  ls /workspace/runpod-slim/ComfyUI/.venv-cu128 >/dev/null 2>&1 "
        "    && echo 'venv: present' || echo 'venv: NOT yet created'; "
        "else "
        "  echo '(workspace ComfyUI dir not yet populated -- still in cp -r?)'; "
        "fi; "
        f"echo '=== last {tail} lines of /jupyter.log ==='; "
        f"tail -n {tail} /jupyter.log 2>/dev/null || echo '(no /jupyter.log)'; "
        f"echo '=== last {tail} lines of /filebrowser.log ==='; "
        f"tail -n {tail} /filebrowser.log 2>/dev/null || echo '(no /filebrowser.log)'; "
    )


def fetch_logs_via_ssh(
    host: str, port: int, image: str, tail: int = 20,
) -> Optional[str]:
    """SSH to the pod and grab the most useful diagnostic info from inside
    the container. Returns stdout on success, None if SSH didn't work.

    `image` is used to pick the right vendor SMI (rocm-smi vs nvidia-smi)
    — without it we'd dump both on every pod and one of them would always
    spew 'command not found' into the log.
    """
    if not config.SSH_LOG_FETCH:
        return None
    remote_cmd = (
        "set +e; "
        "echo '=== uname / hostname ==='; uname -a; hostname; "
        f"echo '=== last {tail} /var/log/syslog lines ==='; "
        f"tail -n {tail} /var/log/syslog 2>/dev/null || echo '(no /var/log/syslog)'; "
        f"echo '=== last {tail} dmesg lines ==='; "
        f"dmesg --no-pager 2>/dev/null | tail -n {tail} || echo '(dmesg unavailable)'; "
        "echo '=== /var/log/*.log tails ==='; "
        "for f in /var/log/*.log; do "
        "  [ -f \"$f\" ] || continue; "
        "  echo \"--- $f ---\"; tail -n 5 \"$f\" 2>/dev/null; "
        "done; "
        + _gpu_smi_block(image)
        + _runtime_state_block(tail)
    )
    cmd = [*_ssh_command_prefix(host, port), remote_cmd]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
    except subprocess.TimeoutExpired:
        return None
    except FileNotFoundError:
        return None
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout
    return f"__SSH_FAILED__\nreturncode={r.returncode}\nstderr: {r.stderr.strip()[:400]}"


def dump_pod_logs(pod_id: str, image: str, tail: int = 20) -> None:
    """Print pod metadata + container logs (via direct SSH) before
    terminating. `image` is forwarded to `fetch_logs_via_ssh` so the
    diagnostic dump only runs the vendor SMI that actually exists on the
    pod (no more 'rocm-smi: command not found' on NVIDIA hosts).
    """
    data = runpodctl_json("pod", "get", pod_id, timeout=30)
    if not isinstance(data, dict):
        log("(could not fetch pod state)", indent=2)
        return
    ssh = data.get("ssh") or {}
    host, port = ssh.get("ip"), ssh.get("port")

    log(f"--- pod metadata for {pod_id} ---", indent=2)
    for key, val in [
        ("desiredStatus",    data.get("desiredStatus")),
        ("uptimeSeconds",    data.get("uptimeSeconds")),
        ("ssh.ip:port",      f"{host}:{port}" if host and port else None),
        ("ssh.error",        ssh.get("error")),
        ("ssh.key_in_account", (ssh.get("ssh_key") or {}).get("in_account")),
        ("imageName",        data.get("imageName")),
        ("lastStatusChange", data.get("lastStatusChange")),
    ]:
        log(f"  {key:20s} = {val!r}", indent=2)

    if not (host and port):
        log("  (no SSH endpoint yet — skipping log fetch)", indent=2)
        log(f"  inspect via UI: https://www.runpod.io/console/pods/{pod_id}", indent=2)
        return

    log(f"--- container/system logs via SSH (root@{host}:{port}) ---", indent=2)
    logs = fetch_logs_via_ssh(host, int(port), image, tail=tail)
    if logs is None:
        log("  (SSH log fetch disabled or ssh binary not found)", indent=2)
        log(f"  inspect via UI: https://www.runpod.io/console/pods/{pod_id}", indent=2)
        return
    if logs.startswith("__SSH_FAILED__"):
        log("  SSH could not reach the pod:", indent=2)
        for line in logs.splitlines()[1:]:
            log(f"    {line}", indent=2)
        log(f"  inspect via UI: https://www.runpod.io/console/pods/{pod_id}", indent=2)
        return
    for line in logs.splitlines():
        log(f"  {line}", indent=2)
