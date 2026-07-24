# Smoke tests for Runpod container images

Spins up each image on a real Runpod pod, waits for it to stay healthy
for `DWELL_SEC` seconds, runs an image-appropriate functional check
(CUDA / `nvidia-smi` / `torch.cuda` / optional JupyterLab / optional
per-port HTTP / optional ComfyUI end-to-end image generation), then
terminates the pod. Designed to catch the failure modes that **only
appear on a real GPU host** and that local `docker run` would miss:
driver-version mismatches, broken NCCL/NVRTC, missing CUDA libs,
`start.sh` regressions, JupyterLab proxy misconfiguration, etc.

```
./test_images.py [path/to/images.yaml] [group_filter]
```

The code is split into a small package next to the entry point:

```
tests/
├── README.md               ← you are here
├── test_images.py          ← entry point: main() + summary + CLI
└── runpod_smoke/
    ├── config.py           ← env vars, sentinels, shared mutable state
    ├── log.py              ← thread-tagged logging
    ├── manifest.py         ← parser + value normalizers
    ├── runpodctl.py        ← subprocess wrappers around the `runpodctl` binary
    ├── instances.py        ← GPU catalog, budget resolution, exclude filter, CUDA detection
    ├── pod.py              ← pod create/lifecycle/signals, registry auth
    ├── checks.py           ← SSH probe, CUDA functional check, Jupyter probes, log dumper
    └── runner.py           ← test_pair / test_image (per-image orchestration)
```


## Prerequisites

1. **Python ≥ 3.9** (stdlib only — no pip install needed).
2. **`runpodctl` 2.3.0+ on `$PATH`**, authenticated:

   ```bash
   runpodctl config --apiKey <YOUR_RUNPOD_API_KEY>
   runpodctl user   # smoke test — should print your account info
   ```

   The API key needs **pod-management** permissions. You can find /
   generate one at <https://www.runpod.io/console/user/settings>.

3. **SSH key registered on your Runpod account.** `test_images.py`
   probes every pod over SSH for the real readiness signal and the
   GPU/CUDA functional check. `runpodctl` writes a managed key pair on
   first use; if you already have one in `~/.runpod/ssh/` you're set.
   To use a different key, point `RUNPOD_SSH_KEY` at the private half
   AND make sure the matching public half is registered at
   <https://www.runpod.io/console/user/settings#ssh-keys>.

4. **(Recommended)** A Docker Hub registry auth registered with
   `runpodctl registry add`. Runpod datacenters share an anonymous Hub
   IP pool that hits the `toomanyrequests` rate limit fast — without
   auth, parallel runs in particular will produce a wave of
   "image pull backoff" failures that look like image bugs but aren't.
   The script auto-discovers the first entry from `runpodctl registry
   list`; pin a specific one with `REGISTRY_AUTH_ID` or
   `REGISTRY_AUTH_NAME`.


## Quick start

Smallest possible manifest — single CPU image:

```yaml
# images-quickstart.yaml
base_cpu:
    images:
    - runpod/base:1.0.6-dev-ubuntu2404
```

Run it:

```bash
./test_images.py images-quickstart.yaml
```

You should see, in order:

1. `discovered N GPU types from runpodctl` — startup catalog query
2. `using registry auth: …` — Docker Hub auth resolved (or a warning)
3. `==================== running 1 job(s) with MAX_PARALLEL=1 ===`
4. `attempt: CPU pod …` → `pod p-xxx created, waiting for RUNNING`
5. `t+Ns endpoint=root@…:NNNN ssh_probe=OK` — pod is up
6. `dwelling 60s and re-probing SSH...`
7. `--- pod metadata for p-xxx ---` + log dump
8. `Cleaning up pod p-xxx...`
9. `===== SUMMARY ===== totals: 1 PASS, 0 FAIL, 0 SKIP`

Exit code is `0` if no `FAIL` and no `SKIP`, `1` otherwise. `SKIP` is
treated as a failure by default because it means no real validation
happened; set `ON_SKIP=warn` (CI: `on-skip: 'warn'`) to keep the job
green with a yellow GitHub Actions warning annotation, or `ON_SKIP=pass`
for the legacy fully-lenient behaviour. See the [Outcomes](#outcomes)
table below.

To test a single group from a larger manifest:

```bash
./test_images.py images.yaml base_cpu
```


## Test lifecycle

For every `(image, instance)` pair the manifest produces, the script
runs this sequence and reports the outcome as soon as one step fails.

| # | Step | Failure → |
|---|------|---|
| 1 | `runpodctl pod create` (with `--gpu-id`, `--container-disk-in-gb`, `--ports`, registry auth, optional `--min-cuda-version`). Transient `5xx` / `Something went wrong` errors are retried silently up to `CREATE_RETRIES` with linear backoff. | `UNAVAILABLE` (no capacity for this instance type — try next) / `CREATE_FAIL` (bad image tag, registry auth, malformed request — any non-capacity, non-transient orchestrator error after retries are exhausted) |
| 2 | Poll `runpodctl pod get` until `ssh.ip` / `ssh.port` are assigned and one-shot `ssh root@ip -p port 'echo ready'` succeeds (the real readiness signal — `desiredStatus` is always `RUNNING` after create) | `STUCK` if no SSH endpoint within `CREATE_TIMEOUT` (almost always a bad host in the scheduler pool — try another instance type) |
| 3 | **CUDA functional check** over SSH — see [Functional check](#functional-check). Image-driven: pytorch ref → `torch.cuda` + matmul; cuda/rocm ref → `nvidia-smi` + `nvcc`; neither → skip | `FAIL` (image is broken — stop iterating; another GPU won't help) |
| 4 | **JupyterLab in-pod check** (only when `test_jupyter: true`) — see [Jupyter check](#jupyter-check-opt-in). SSH in, wait for `:8888` to bind, `jupyter server list`, `curl /api/status` with token | `FAIL` (`start.sh` didn't bring up Jupyter — usually wrong python interpreter) |
| 5 | **JupyterLab public-proxy check** (only when `test_jupyter: true`) — `GET https://<pod-id>-8888.proxy.runpod.net/api/status` from the test machine | `FAIL` (port not exposed as `8888/http`, or proxy never registered) |
| 6 | **Per-port HTTP checks** (only when `test_ports: [...]`) — see [Per-port checks](#per-port-checks-opt-in). For every listed port: SSH in and `curl http://127.0.0.1:<port>/`, then `GET https://<pod-id>-<port>.proxy.runpod.net/` from the test machine. | `FAIL` (service didn't bind, returned `5xx`, or port wasn't exposed as `<port>/http` so the proxy never registered it) |
| 7 | **ComfyUI reachability smoke** (when `test_comfyui: true`, also implied by `test_comfyui_functional`) — see [ComfyUI checks](#comfyui-checks-smoke--functional). Probe `:8188` in-pod (`curl 127.0.0.1:8188`) then via the public proxy. | `FAIL` (ComfyUI didn't bind, returned `5xx`, or `:8188` wasn't exposed as `8188/http`) |
| 8 | **ComfyUI functional check** (only when `test_comfyui_functional: true`) — see [ComfyUI checks](#comfyui-checks-smoke--functional). Host-side against the public proxy URL (no SSH): provision the model(s) via ComfyUI-RunpodDirect's `/server_download/*` routes, POST the workflow to `/prompt`, poll `/history`, fetch the output via `/view` and validate it's a real PNG. Runs only after the reachability smoke (7) passes. | `FAIL` (couldn't provision the model, ComfyUI rejected the workflow, generation errored/timed out, or no valid PNG came out) |
| 9 | Sleep `DWELL_SEC`, re-probe SSH (catches "boots fine then crashes after 30s") | `FAIL` if SSH stops responding |
| 10 | `dump_pod_logs` — pull `uname`, `syslog`, `dmesg`, `/var/log/*.log`, `nvidia-smi` via SSH for the run log | _(diagnostic only)_ |
| 11 | `runpodctl pod delete` (always — even on Ctrl-C / exception via `atexit` + signal handlers) | _(diagnostic only)_ |

`test_image()` then iterates over the next instance candidate when the
result was `UNAVAILABLE` or `STUCK`, and short-circuits on `PASS`,
`FAIL`, or `CREATE_FAIL` — UNLESS the group sets `check_all_gpu: true`,
in which case `_build_jobs` emits one job per `(image, instance)` pair
up-front and every pair is tested independently regardless of outcome
(see [Per-GPU compatibility matrix](#per-gpu-compatibility-matrix-check_all_gpu)).


## Outcomes

The summary at the end of every run groups results into three buckets.
The granular per-pod outcomes below collapse into them:

| summary | per-pod outcome | what it means | what to do |
|---|---|---|---|
| `PASS` | `PASS` | Image booted, all checks passed, survived dwell. | nothing |
| `FAIL` | `FAIL` | Pod was created and the container itself proved broken (CUDA check failed, JupyterLab didn't start, crashed during dwell, etc.). Moving to another GPU won't help — the image is the problem. | fix the image |
| `FAIL` | `CREATE_FAIL` | Pod-create returned a non-capacity, non-transient orchestrator error (bad image tag, registry auth, malformed request, missing CUDA version). | fix the manifest / image ref / auth |
| `SKIP` | all `UNAVAILABLE` | Runpod had no capacity on **any** candidate instance type. | retry later, expand `instances:` list, or raise `max_price_per_hour` |
| `SKIP` | some `STUCK` + rest `UNAVAILABLE` | At least one instance was scheduled but Runpod never assigned an SSH endpoint within `CREATE_TIMEOUT` (slow pull / dead host). | retry later — usually transient |

`FAIL` always exits `1`. `SKIP` is governed by `ON_SKIP` (env-var) /
`on-skip` (CI input), one of:

* `fail` (default) — exit `1` + `::error::` annotation. Job goes red.
* `warn`           — exit `0` + `::warning::` annotation. Job stays
  green; the run shows a yellow warning bubble in the PR check tab.
* `pass`           — exit `0`, no annotation (legacy lenient mode).

Unknown values silently coerce to `fail` so a typo never disables the
safer default. The summary lists the **instance that produced the outcome** in brackets,
so you can tell whether a `FAIL` correlates with a specific GPU type:

```
================================== SUMMARY =================================
totals: 4 PASS, 1 FAIL, 1 SKIP

  FAIL   runpod/pytorch:…cu1300-torch260… [RTX 5090] -- CUDA/GPU functional check failed
  SKIP   runpod/base:…rocm644-ubuntu2404… -- no capacity on any of 1 candidate instance type(s)
  PASS   runpod/base:…ubuntu2404 [CPU]
  PASS   runpod/base:…cuda1281-ubuntu2204 [RTX A4000]
  PASS   runpod/base:…cuda1281-ubuntu2404 [RTX A5000]
  PASS   runpod/base:…cuda1300-ubuntu2404 [RTX 4090]
```

For `check_all_gpu: true` groups the same image appears as **multiple
rows** — one per `(image, instance)` pair tested — so you get a
compatibility matrix:

```
================================== SUMMARY =================================
totals: 4 PASS, 2 FAIL, 0 SKIP

  FAIL   runpod/comfyui:cuda12.8 [RTX 5090]     -- port 8188 check failed (in-pod)
  FAIL   runpod/comfyui:cuda12.8 [RTX PRO 6000 Blackwell] -- port 8188 check failed (in-pod)
  PASS   runpod/comfyui:cuda12.8 [RTX 4090]
  PASS   runpod/comfyui:cuda12.8 [RTX A5000]
  PASS   runpod/comfyui:cuda13.0 [RTX 5090]
  PASS   runpod/comfyui:cuda13.0 [RTX 4090]
```


## Common invocations

```bash
# Default manifest path is ./images, group filter is none.
./test_images.py

# Explicit manifest path
./test_images.py /path/to/my-images.yaml

# Only one group from a multi-group manifest
./test_images.py images.yaml pytorch

# Run 3 images in parallel (caps live pods at 3)
MAX_PARALLEL=3 ./test_images.py images.yaml

# Skip the 60s post-boot dwell to get faster iterations during debugging
DWELL_SEC=0 ./test_images.py images.yaml base_cpu

# Use a non-default SSH key
RUNPOD_SSH_KEY=~/.ssh/my_runpod_key ./test_images.py images.yaml

# Pin to a specific registry auth (avoid auto-pick when you have several)
REGISTRY_AUTH_NAME='dockerhub-prod' ./test_images.py images.yaml
# …or by id
REGISTRY_AUTH_ID='clxxxxxxxxxx' ./test_images.py images.yaml

# Keep the job green on SKIP but surface a yellow warning (GitHub
# Actions warning annotation in the PR check tab).
ON_SKIP=warn ./test_images.py images.yaml

# Fully lenient — script exits 0 on SKIP with no annotation.
ON_SKIP=pass ./test_images.py images.yaml
```

If a pod gets stuck (rare), `Ctrl-C` cleans up — `SIGINT`/`SIGTERM` are
trapped and trigger `cleanup_all()`, which `runpodctl pod delete`s
every pod the script created. Any pod the script missed will still
self-terminate within ~2 h via the `--terminate-after` clause set on
every `pod create`.


## Manifest schema

```yaml
groupname:
    images:                # list of docker images to test (required)
    - imagename
    instances:             # explicit list of GPU display names, priority order
    - "RTX A4000"
    max_price_per_hour: 1.0   # OR budget filter — auto-pick cheapest first
    check_all_gpu: true       # OR test on EVERY GPU in the catalog (diagnostic mode)
    min_vram_gb: 16           # extra filter for budget / check_all_gpu mode
    manufacturer: Nvidia      # 'Nvidia' or 'AMD' filter for budget / check_all_gpu mode
    exclude_instances:        # subtract fnmatch patterns from candidates
    - "*Blackwell*"
    min_cuda_version: "13.0"  # 'X.Y' string for --min-cuda-version (fallback only)
    test_jupyter: true        # opt-in JupyterLab in-pod + proxy check
    test_ports:               # opt-in generic per-port HTTP check (in-pod + proxy)
    - 8188                    #   one row per (image, instance, port) is exercised
    - 8888
    - 8080
    test_comfyui: true             # ComfyUI reachability smoke on :8188
    test_comfyui_functional: true  # ComfyUI end-to-end generate-image check
```

Field reference:

| field | description |
|---|---|
| `images` | Docker images to test. **Required.** |
| `instances` | Explicit list of GPU display names, tried in order. One of `instances:`, `max_price_per_hour:`, or `check_all_gpu:` is required (except for `base_cpu`). |
| `max_price_per_hour` | USD/hr budget — auto-pick any GPU at this price or below, cheapest first. Loses to explicit `instances:` if both are set. |
| `check_all_gpu` | `true` / `false` — when true and there's no explicit `instances:`/`max_price_per_hour:`, ALL GPUs from the catalog are picked (after `manufacturer` / `min_vram_gb` filtering). ALSO disables the "stop iterating on PASS" short-circuit in the runner, so every `(image, instance)` pair is tested and shows up as its own row in the summary. Designed for diagnostic runs ("on which GPUs does this image work?"). See [Per-GPU compatibility matrix](#per-gpu-compatibility-matrix-check_all_gpu). Default: `false`. |
| `min_vram_gb` | Extra filter for budget mode and `check_all_gpu` mode (default 0). |
| `manufacturer` | `Nvidia` or `AMD` filter for budget mode and `check_all_gpu` mode (default: any). |
| `exclude_instances` | fnmatch-style patterns (case-insensitive) subtracted from the candidate list AFTER `instances:`, budget, or `check_all_gpu` selection. Useful for blocking known-bad host pairings without rewriting the whole list — e.g. `"*Blackwell*"` skips every Blackwell GPU (sm\_100 / sm\_120 are not in the kernel set of PyTorch ≤ 2.6 wheels). |
| `min_cuda_version` | `X.Y` string passed to `runpodctl pod create --min-cuda-version`. Only used as a **fallback** when the image tag itself doesn't encode a CUDA version (e.g. NGC `nvidia-pytorch:25.11`). Image tags like `cu1281` / `cuda1281` always win. |
| `test_jupyter` | `true` / `false` — when true, the pod is created with `JUPYTER_PASSWORD=admin` in env and HTTP port 8888 exposed, then the script SSHes in and verifies JupyterLab is actually listening **with Jupyter-specific assertions** (`jupyter server list`, `/api/status` with token). Use for groups whose images use `container-template/start.sh` (`runpod/base`, `runpod/pytorch`, `runpod/autoresearch`, `rocm`). Skip for NGC `nvidia-pytorch` (different entrypoint). Default: `false`. |
| `test_ports` | List of TCP ports the image is expected to serve over HTTP. Each port is exposed as `<port>/http` so Runpod's public proxy registers it, then the runner probes the port twice: (1) in-pod via SSH (`curl http://127.0.0.1:<port>/`), (2) via the public proxy (`https://<pod-id>-<port>.proxy.runpod.net/`). Generic counterpart to `test_jupyter` — no app-specific assertions, just "a server responds with HTTP `<500`". Use for ComfyUI (`8188`), FileBrowser (`8080`), or any app where you only need to verify "it's listening". Can coexist with `test_jupyter: true` (Jupyter on 8888 is still checked with the Jupyter-specific probes; any other port in `test_ports` gets the generic one). Default: empty. |
| `test_comfyui` | `true` / `false` — ComfyUI **reachability smoke**. A ComfyUI-branded alias for `test_ports: [8188]`: exposes `:8188` as `8188/http` and probes it twice — in-pod (`curl 127.0.0.1:8188`) and via the public Runpod proxy (`https://<pod-id>-8188.proxy.runpod.net/`). Accepts any HTTP `<500`. Answers **"is ComfyUI up and reachable from a browser?"** — not whether it can generate. Cheap (no download, no GPU work). Also enabled implicitly by `test_comfyui_functional`. Default: `false`. |
| `test_comfyui_functional` | `true` / `false` — ComfyUI **end-to-end functional check**. Proves the image can actually **generate an image**, run **host-side against the public proxy URL** (`https://<pod-id>-8188.proxy.runpod.net`, no SSH): provisions the checkpoint(s) from [`tests/comfyui/models.json`](comfyui/models.json) via the baked-in [ComfyUI-RunpodDirect](https://github.com/MadiatorLabs/ComfyUI-RunpodDirect) node's `/server_download/*` routes, POSTs the workflow [`tests/comfyui/workflows/gsl_starter_1_1.api.json`](comfyui/workflows/gsl_starter_1_1.api.json) (the "1.1 Starter – Text to Image" template) to `/prompt`, polls `/history`, then fetches the result via `/view` and asserts it's a real, non-empty PNG. **Implies `test_comfyui`** — the reachability smoke runs first and the generation only runs if it passes (no point spending GPU time on an unreachable ComfyUI). Heavier: pulls a ~2 GB model + uses GPU time, so gate it behind an enabler. Default: `false`. |

The `base_cpu` group is special: `runpodctl` 2.3.0 does not let us pick
a specific CPU flavor (`--gpu-id` is rejected for `--compute-type CPU`),
so the manifest needs ONLY an `images:` list for that group — no
`instances:` / `max_price_per_hour:` / `min_vram_gb:`. Runpod picks a
CPU flavor for us.


## Example manifest (the real one used in this repo)

Lives outside the repo at `~/tmp/runpod-scripts/testing/images` — the
manifest is environment-specific (image tags depend on which branch
you're testing). Annotated example covering every supported pattern:

```yaml
# CPU-only base image — no instances:, no budget, just images.
base_cpu:
    images:
    - runpod/base:1.0.6-dev-ubuntu2204
    - runpod/base:1.0.6-dev-ubuntu2404
    test_jupyter: true              # base CPU image still ships JupyterLab

# GPU base image, budget-selected. The CUDA functional check is auto-
# applied because the tag contains 'cuda1281' / 'cuda1290' / 'cuda1300'.
base_gpu:
    images:
    - runpod/base:1.0.6-dev-cuda1281-ubuntu2204
    - runpod/base:1.0.6-dev-cuda1281-ubuntu2404
    - runpod/base:1.0.6-dev-cuda1300-ubuntu2404
    max_price_per_hour: 1.0
    min_vram_gb: 16
    manufacturer: Nvidia
    test_jupyter: true

# autoresearch — torch lives in /opt/autoresearch/.venv, NOT importable
# from system python. The image-driven check picks nvidia-smi (because
# tag has 'cuda' but no 'pytorch' / 'torch\d' marker), which is what
# we want — we'd never get a clean torch import over SSH otherwise.
autoresearch:
    images:
    - runpod/autoresearch:1.0.6-dev-cuda1281-ubuntu2204
    - runpod/autoresearch:1.0.6-dev-cuda1281-ubuntu2404
    max_price_per_hour: 1.0
    min_vram_gb: 16
    manufacturer: Nvidia
    test_jupyter: true

# NGC base image. Tag '25.11' encodes no CUDA version — without
# min_cuda_version the scheduler picks any host and the container
# fails at startup with `nvidia-container-cli: cuda>=13.0`.
nvidia-pytorch:
    images:
    - runpod/nvidia-pytorch:1.0.6-dev-25.11
    max_price_per_hour: 1.0
    min_vram_gb: 16
    manufacturer: Nvidia
    min_cuda_version: "13.0"        # NGC 25.09+ PyTorch is built on cu13.0
    # No test_jupyter — NGC uses its own entrypoint, not our start.sh.

# AMD ROCm — explicit instance list because only MI300X carries ROCm.
rocm:
    images:
    - runpod/base:1.0.6-dev-rocm644-ubuntu2204-py310-pytorch251
    - runpod/base:1.0.6-dev-rocm644-ubuntu2404-py312-pytorch271
    instances:
    - MI300X
    test_jupyter: true

# runpod/pytorch — torch in system python, full torch.cuda check runs.
# PyTorch ≤ 2.6 wheels ship kernels only for sm_50…sm_90; Blackwell GPUs
# are sm_100/sm_120, so booting on one of them gives "no kernel image
# is available for execution on the device". Filter them out:
pytorch:
    images:
    - runpod/pytorch:1.0.6-dev-cu1281-torch260-ubuntu2204
    - runpod/pytorch:1.0.6-dev-cu1300-torch260-ubuntu2404
    max_price_per_hour: 1.0
    min_vram_gb: 16
    manufacturer: Nvidia
    test_jupyter: true
    exclude_instances:
    - "*Blackwell*"
```


## Environment variables

| var | default | description |
|---|---|---|
| `CLOUD_TYPE` | `SECURE` | `SECURE` or `COMMUNITY`. |
| `DISK_GB` | `100` | Container disk size for GPU pods. |
| `CPU_DISK_GB` | `20` | Container disk size for CPU pods. Runpod caps this per CPU flavor (20 GB on the cheapest, 30 GB on larger ones); 20 is the universal safe value. |
| `CPU_CANDIDATES` | `""` (uses `cpu-secure,cpu-community`) | CPU "instance candidates". `runpodctl pod create` doesn't accept `--vcpu` / `--mem` / `--cpu-flavor`, so we vary the axes it DOES expose for CPU: `--cloud-type` (SECURE vs COMMUNITY) and optional `--data-center-ids`. Each label becomes one candidate iterated by the same per-instance retry loop GPU groups use, so when SECURE is saturated COMMUNITY almost always has free CPU capacity. Format: `label:CLOUD[:DC1+DC2+…],label:CLOUD[:DC_CSV],…` (use `+` not `,` to separate DC ids inside one candidate so the outer csv stays unambiguous). CLOUD must be SECURE or COMMUNITY. Malformed entries are silently dropped; an empty/all-broken value falls back to the default 2-candidate list. |
| `RUNPOD_API_KEY` | _(from `~/.runpod/config.toml`)_ | Used for the GraphQL GPU pricing query. Set this in CI / containers without a config file. |
| `REGISTRY_AUTH_ID` | _(empty)_ | Explicit Docker Hub registry auth id to pass as `--registry-auth-id`. Overrides auto-discovery. |
| `REGISTRY_AUTH_NAME` | _(empty)_ | Display name to look up via `runpodctl registry list` when `REGISTRY_AUTH_ID` is not set. Falls back to the first entry. |
| `DWELL_SEC` | `60` | Extra seconds to wait after SSH becomes reachable, then re-probe SSH to catch containers that boot, accept SSH, then crash. Set 0 to skip the re-probe. |
| `CREATE_TIMEOUT` | `600` | Max seconds to wait for SSH to become reachable. Raise for ROCm workflows (`create-timeout: "1200"` on the action) — the official `rocm/pytorch:*` base images are 30-50GB and routinely take 8-15 minutes to pull. |
| `POLL_INTERVAL` | `10` | Poll cadence for SSH probes. |
| `MAX_PARALLEL` | `1` | How many images to smoke-test concurrently. Each worker holds at most one pod, so this caps simultaneous live pods. Keep modest to avoid Runpod rate limits and surprise bills. |
| `CREATE_RETRIES` | `3` | Retry pod-create up to N times on transient Runpod 5xx errors (`Something went wrong`, 502/503). Capacity shortages are NOT retried. |
| `CREATE_RETRY_BACKOFF` | `10` | Seconds between retries (linear backoff). |
| `STALL_HINT_AFTER` | `180` | Seconds without an SSH endpoint before the script prints a hint about slow pulls / possible Docker Hub rate limit. |
| `SSH_LOG_FETCH` | `1` | `1`/`0` — fetch container logs via direct SSH at PASS/FAIL. |
| `RUNPOD_SSH_KEY` | _(empty)_ | Path to private key matching the `PUBLIC_KEY` `runpodctl` injects into pods. Auto-discovered from common locations if not set. |
| `JUPYTER_WAIT_TIMEOUT` | `30` | Seconds the in-pod Jupyter probe waits for `:8888` to bind. |
| `JUPYTER_PROXY_TIMEOUT` | `60` | Seconds the proxy probe retries while Runpod's ingress registers the new pod. |
| `PORT_WAIT_TIMEOUT` | `300` | Seconds the in-pod `test_ports` probe waits for a port to bind on `127.0.0.1` AND return an HTTP `<500` response (single unified retry loop, heartbeat every 30s). Fast apps (Jupyter, FileBrowser) exit in <2s. Bump to `900` for ComfyUI cold starts — first-boot `cp -r` of ~8 GB into `/workspace` plus ComfyUI-Manager FETCH can push readiness past 5 minutes. |
| `PORT_PROXY_TIMEOUT` | `300` | Seconds the public-proxy `test_ports` probe retries waiting for Runpod's ingress to register the new pod. Same override pattern as `PORT_WAIT_TIMEOUT` — bump together when testing slow apps. |
| `COMFYUI_PORT` | `8188` | Port the ComfyUI HTTP API listens on. The `test_comfyui` reachability probe hits it in-pod + via proxy; the `test_comfyui_functional` check uses it to build the public proxy URL `https://<pod-id>-<port>.proxy.runpod.net`. |
| `COMFYUI_WORKFLOW` | `tests/comfyui/workflows/gsl_starter_1_1.api.json` | Path to the ComfyUI **API-format** workflow POSTed to `/prompt`. Override to test a different template. |
| `COMFYUI_MODELS_MANIFEST` | `tests/comfyui/models.json` | Path to the JSON list of models to provision before running (`filename`, `directory` = a ComfyUI `folder_paths` key, `url`, `sha256`). |
| `COMFYUI_WAIT_TIMEOUT` | `600` | Seconds the `test_comfyui_functional` probe waits for `/system_stats` to answer **through the proxy** (cold ComfyUI cp -r + torch import + Manager fetch + eventually-consistent proxy). |
| `COMFYUI_DOWNLOAD_TIMEOUT` | `900` | Seconds allowed for RunpodDirect to provision the model(s). DreamShaper 8 pruned is ~2.1 GB. |
| `COMFYUI_GEN_TIMEOUT` | `300` | Seconds allowed for the generation itself (queue → PNG on disk), including the cold first checkpoint load into VRAM. |
| `COMFYUI_SAVE_DIR` | _(empty)_ | Local directory to save the generated PNG into (a plain HTTP GET of `/view`, then written as `<pod-id>_<filename>.png`). Empty = validate from the `/view` response only, don't keep a copy (keeps CI stdout light). Set it to actually **see** the image — the pod is deleted right after the check. |


## Functional check

Runs over SSH after the container is reachable. **The check is selected
by inspecting the image REF, not the manifest group name** — so new
groups don't silently skip the check:

- image has `rocm` in ref
  → `rocm-smi` GPU enumeration + optional `hipcc --version`. Matched
  first so ROCm-pytorch images (built from `rocm/pytorch:*` where torch
  lives in a conda env not visible to the system `python`) don't get
  routed into the torch-import path and falsely fail with
  `ModuleNotFoundError`.
- image has `pytorch` / `torch\d` in ref
  → `torch.cuda.is_available` + matmul on device (catches broken drivers,
  missing libs, mismatched toolkit/driver versions). NVIDIA only at this
  point — ROCm was already handled above.
- image has `cuda` / `cu\d` (but no torch markers)
  → `nvidia-smi -L` + driver/memory query + `nvcc --version`. Covers base
  GPU images and `autoresearch` (whose torch is in a venv not reachable
  from the system Python we SSH into).
- otherwise (no GPU markers)
  → no check. Pod must still boot and survive `DWELL_SEC`.


## Jupyter check (opt-in via manifest `test_jupyter: true`)

Two stages, both must pass:

1. **In-pod.** SSH into the pod and `curl http://127.0.0.1:8888/api/status`
   with our token. Catches silent `start.sh` failures (e.g. `python3 -m
   jupyter` not finding the module on Ubuntu 22.04 — the kind of bug
   that prints `Jupyter Lab started` in the container log while no
   server is actually running).
2. **Public proxy.** From the test machine, `GET
   https://<pod-id>-8888.proxy.runpod.net/api/status` with the token.
   Catches port-type misconfigurations (`8888/tcp` instead of
   `8888/http` — the proxy never wires up non-http ports) and DNS /
   proxy registration issues that would prevent real users from
   reaching Jupyter from the Runpod console.


## Per-port checks (opt-in via manifest `test_ports: [...]`)

Generic counterpart to the Jupyter check — verifies that **some** HTTP
server binds each listed port and answers, both locally and through
Runpod's public proxy. No app-specific assertions, so it's the right
tool for ComfyUI (`8188`), FileBrowser (`8080`), Tensorboard, etc.

For every port in the list, two probes run in sequence (both must pass):

1. **In-pod.** SSH in and run a single unified retry loop for up to
   `PORT_WAIT_TIMEOUT` seconds: at each iteration probe `/dev/tcp/127.0.0.1/<port>`
   for binding, and if the port is open also try `curl http://127.0.0.1:<port>/`.
   The probe **accepts any HTTP status `<500`** (200, 301, 401, 403 all
   prove the server is alive — many apps return 401/403 on `/` without
   auth and that's still a "the service is up" signal we want to see).
   Output streams live to the host with a heartbeat every 30s so long
   warm-up windows (ComfyUI cold start, etc.) don't look frozen.
   Catches "service never started", "service died on first request",
   and "service bound to a non-loopback interface".
2. **Public proxy.** From the test machine, `GET
   https://<pod-id>-<port>.proxy.runpod.net/`. Same `<500` acceptance
   criterion. Catches the most common end-user-facing failure mode:
   the port was declared `<port>/tcp` (or not declared at all) so
   Runpod's proxy never registered it — the in-pod probe would still
   pass, but real users can't reach the service from a browser.

Independent from `test_jupyter`. You can enable both — port 8888 will
go through Jupyter-specific probes (server list + token), and any
other port in `test_ports` gets the generic check. If `test_ports`
also includes 8888, the generic check runs in addition to the Jupyter
one (cheap insurance — they probe slightly different aspects).


## ComfyUI checks (smoke + functional)

There are **two** ComfyUI-specific flags, from cheap to thorough. They are
separate manifest fields, but the functional one implies the smoke one:

| flag | tier | what it proves | cost |
|---|---|---|---|
| `test_comfyui: true` | smoke | ComfyUI is **up and reachable** on `:8188` (in-pod + public proxy) | seconds, no GPU work |
| `test_comfyui_functional: true` | functional | ComfyUI can **actually generate an image** | pulls ~2 GB model + GPU time |

**`test_comfyui` (reachability smoke).** A ComfyUI-branded alias for
`test_ports: [8188]`: exposes `:8188` as `8188/http`, then probes it
in-pod (`curl 127.0.0.1:8188`) and through the public Runpod proxy,
accepting any HTTP `<500`. It only answers "is the server up and reachable
from a browser?". Use it on every ComfyUI image — it's cheap. (You don't
also need `test_ports: [8188]`; this replaces it. Keep `test_ports` for
*other* ports like `8080` FileBrowser.)

**`test_comfyui_functional` (end-to-end).** This answers the question that
actually matters for a ComfyUI image: **can a user log in, pick a template,
pull the missing model, run it, and get a picture out?** It mirrors that
exact flow over the ComfyUI HTTP API, run **entirely host-side against the
public proxy URL** (`https://<pod-id>-8188.proxy.runpod.net`) — **no SSH and
no in-pod script**. Model provisioning uses the
[ComfyUI-RunpodDirect](https://github.com/MadiatorLabs/ComfyUI-RunpodDirect)
custom node baked into the image, whose `/server_download/*` routes live on
the same ComfyUI server as `/prompt`, so the whole flow is reachable through
the proxy — exactly like the **"Download to Pod"** button in ComfyUI's
missing-models dialog. It **implies `test_comfyui`**: the reachability smoke
above runs first, so the functional check reuses a proxy path it already knows
is up (and we never spend GPU time on an unreachable ComfyUI). Because it's
heavy, gate it behind an enabler (see [Running in CI](#running-in-ci)).

The two shipped assets define the functional flow:

- [`tests/comfyui/workflows/gsl_starter_1_1.api.json`](comfyui/workflows/gsl_starter_1_1.api.json)
  — the "1.1 Starter – Text to Image" template exported from ComfyUI in
  **API format** (the shape `/prompt` accepts). It's a minimal SD1.5
  graph: `CheckpointLoaderSimple` → `CLIPTextEncode` ×2 → `KSampler` →
  `VAEDecode` → `SaveImage`. The seed is fixed so runs are reproducible.
- [`tests/comfyui/models.json`](comfyui/models.json) — the checkpoint(s)
  the workflow needs but the image doesn't bake in. Each entry has a
  `filename`, target `directory` (a ComfyUI `folder_paths` key, e.g.
  `checkpoints`), download `url`, and expected `sha256`. The starter
  template needs `DreamShaper_8_pruned.safetensors` (~2.1 GB).

Steps the host-side probe runs (any failure ⇒ `FAIL` on the pair, with the
failing reason surfaced):

1. **Wait for the API.** Poll `/system_stats` **through the proxy** until it
   answers (up to `COMFYUI_WAIT_TIMEOUT` — generous, because a cold container
   copies ~8 GB of baked ComfyUI into `/workspace` and imports torch before
   `:8188` binds, and the proxy itself is eventually-consistent).
2. **Provision the model.** For each entry in `models.json`: first
   `POST /server_download/verify_model_integrity` — if the file already
   exists (and its sha256 matches, when known) the download is skipped.
   Otherwise `POST /server_download/start` (RunpodDirect writes into the
   right `folder_paths` dir with an 8-connection download and verifies
   size + sha256 server-side), then poll `GET /server_download/status/...`
   until `completed`. Requires the RunpodDirect routes to exist — if
   `GET /server_download/folder_paths` 404s (node missing from the image),
   the check fails with a clear message rather than silently.
3. **Confirm visibility.** Hit `/object_info/CheckpointLoaderSimple` and
   assert the freshly-downloaded checkpoint now shows up in the node's
   enum (retries briefly to absorb the rescan lag).
4. **Queue the workflow.** `POST /prompt` with the graph. A non-empty
   `node_errors` (or an HTTP 4xx with a validation body) fails fast with
   ComfyUI's own error text.
5. **Wait for the result.** Poll `/history/<prompt_id>` until the run
   reports `success` with image outputs, or `error`, or
   `COMFYUI_GEN_TIMEOUT` elapses.
6. **Validate the PNG.** `GET /view?...` for the first output and assert
   it starts with the PNG magic bytes, is non-trivially sized, and has a
   sane width/height in the IHDR — i.e. a real image, not an error page.

Progress streams live to the host (download + generation take minutes), so
the run never looks frozen. Because everything is bounded HTTP polling with
per-request timeouts, there's no risk of a hung SSH pipe leaking a pod. To
point the check at a different template / model set without touching code,
override `COMFYUI_WORKFLOW` / `COMFYUI_MODELS_MANIFEST` (both are paths) —
see the env table below.

```bash
# Run just the ComfyUI functional check against a specific image tag.
# (edit tests/comfyui/images.example.yaml to set your real tag first)
./test_images.py tests/comfyui/images.example.yaml comfyui

# Same, but fetch the generated PNG so you can eyeball it. The pod is
# terminated right after the check, so this env var is the only way to
# keep a copy — it lands at ./comfy-out/<pod-id>_smoke_00001_.png.
COMFYUI_SAVE_DIR=./comfy-out ./test_images.py tests/comfyui/images.example.yaml comfyui
```

By default the check validates the PNG (magic bytes + IHDR dimensions)
straight from the `/view` response and doesn't keep it — CI only needs the
pass/fail signal. `COMFYUI_SAVE_DIR` opts into writing it to disk (a plain
HTTP GET, no base64/SSH) for local inspection.


## Per-GPU compatibility matrix (`check_all_gpu`)

Default mode tests each image on the manifest's candidate list and
**stops at the first PASS** — the assumption is "if it works on any
host of this family, it works". That's the right default for CI smoke
tests but useless when the actual question is "show me **every** GPU
this image works on / doesn't work on" (e.g. tracking down which
Blackwell variants break a PyTorch wheel, or which Ada cards exhibit
a driver-version regression).

`check_all_gpu: true` switches the group into matrix mode:

1. **Instance selection.** When neither `instances:` nor
   `max_price_per_hour:` is set, the runner pulls **every** GPU from
   the Runpod catalog (`gpuTypes` GraphQL query), then applies the
   usual `manufacturer:` / `min_vram_gb:` / `exclude_instances:`
   filters. With an explicit list / budget, those still win — so you
   can intentionally narrow the matrix.
2. **No PASS short-circuit.** `_build_jobs` emits one job per `(image,
   instance)` pair up front. The runner tests each independently and
   never aborts the rest of the matrix on a PASS or a FAIL. The
   summary then carries one row per pair.

This is intentionally diagnostic-only — for an image with 2 tags and
~30 NVIDIA GPUs in the catalog at ~5 min/pod, a run takes 5 h serial
or ~1.5 h at `MAX_PARALLEL=3`. Plan accordingly:

```bash
# Run on every NVIDIA GPU, 3 pods at a time. Will take hours and cost
# real money — make sure ON_SKIP / capacity expectations match.
MAX_PARALLEL=3 ./test_images.py comfyUI_images
```


## Running in CI

The composite action at
[`.github/actions/smoke-test/action.yml`](../.github/actions/smoke-test/action.yml)
wraps everything in this script needs for a clean CI run:

1. Installs the pinned `runpodctl` binary (`runpodctl-version`,
   `runpodctl-sha256` inputs).
2. Configures the Runpod API key (`runpod-api-key` input) into
   `~/.runpod/config.toml`.
3. Writes the `ssh-private-key` input to `~/.ssh/id_runpod` and exports
   `RUNPOD_SSH_KEY` so the in-pod CUDA probe and log fetch work.
4. Generates a manifest from the `image-refs` JSON array using
   `.github/scripts/generate_test_manifest.py`, applying the
   `profile`, `budget-usd-per-hour`, `min-vram-gb`, `manufacturer`,
   `test-jupyter`, `test-comfyui`, `test-comfyui-functional`, `test_ports`,
   and `exclude-instances` inputs.
5. Invokes `python3 tests/test_images.py <generated-manifest>` with
   `MAX_PARALLEL=<max-parallel>` and `continue-on-error: true` so a
   single broken image doesn't take the whole pipeline down.

Typical caller (from a per-image-family build workflow):

```yaml
- uses: ./.github/actions/smoke-test
  with:
    image-refs: ${{ toJSON(steps.bake.outputs.image-refs) }}
    profile: gpu                            # base = split CPU/GPU (only for runpod/base) | gpu = single base_gpu group (everything else)
    runpod-api-key: ${{ secrets.RUNPOD_API_KEY }}
    ssh-private-key: ${{ secrets.RUNPOD_SSH_KEY }}
    budget-usd-per-hour: "1.0"
    min-vram-gb: "16"
    manufacturer: Nvidia
    test-jupyter: "true"
    exclude-instances: |
      *Blackwell*
    max-parallel: "3"
```

The full input reference lives in the action's own `description:`
fields.

**The ComfyUI functional test is opt-in.** By default CI runs the smoke
checks (boot + CUDA + Jupyter + ComfyUI reachability on `:8188` +
FileBrowser on `:8080`). The heavier functional check
(`test_comfyui_functional` — download the ~2 GB model + generate an image
on a real GPU) is gated behind an enabler:

* **Dev builds** (`.github/workflows/dev.yml`) always run the ComfyUI
  reachability smoke (`test-comfyui: true`) and expose a
  `run_functional_tests` boolean on the `workflow_dispatch` form — leave
  it unchecked for smoke-only, tick it to also run the functional test.
  It maps straight to the action's `test-comfyui-functional` input.
* **Releases** (`.github/workflows/release.yml`) always run the full
  functional check (`test-comfyui-functional: "true"`) and archive the
  generated image (`save-comfyui-images: "true"`) — a release gates on the
  image actually being able to generate.
* **Incompatibility matrix** (`.github/workflows/check-incompatibilities.yml`)
  runs the functional check on **every** resolved GPU (`check_all_gpu: true`
  + `test-comfyui-functional: "true"`) and uploads all the per-GPU PNGs to
  the `comfyui-generated-images` artifact — that's how it surfaces
  GPU-specific generation failures (e.g. missing Blackwell kernels).
* To enable the functional check in any other workflow, pass
  `test-comfyui-functional: "true"` to the `smoke-test` action (it implies
  `test-comfyui`, so reachability is covered automatically).

**Inspecting the generated images in CI.** The pod is deleted right after
the check, so to actually *see* what came out, the `smoke-test` action can
upload the PNG(s) as a **workflow artifact** (never committed to the repo):
pass `save-comfyui-images: "true"` and it sets `COMFYUI_SAVE_DIR`, then
uploads the result as the `comfyui-generated-images` artifact (download it
from the run's Summary page). It uploads on `always()`, so even a failed
generation surfaces whatever it produced. `dev.yml` wires this to the same
`run_functional_tests` toggle, so ticking that box both runs the functional
test and archives its images. Tune the artifact with
`comfyui-images-artifact-name` (give parallel calls distinct names) and
`comfyui-images-retention-days` (default 14).


## Troubleshooting

| symptom in logs | likely cause | fix |
|---|---|---|
| `runpodctl not found in PATH` | `runpodctl` binary missing | install from <https://github.com/runpod/runpodctl/releases>, put on `$PATH` |
| `runpodctl is not authenticated. Run 'runpodctl doctor'` | API key not configured or expired | `runpodctl config --apiKey <KEY>` |
| `warn: no GPU pricing data` | `RUNPOD_API_KEY` not set and no `~/.runpod/config.toml` | set `RUNPOD_API_KEY` or run `runpodctl config --apiKey` |
| `warn: no registry auth configured` | no Docker Hub auth registered | `runpodctl registry add` (paid Hub account strongly recommended for parallel runs) |
| every group says `no capacity on any of N candidate instance type(s)` | budget too low / VRAM too high / region saturated | raise `max_price_per_hour`, drop `min_vram_gb`, or set explicit `instances:` |
| only the `base_cpu` group says `no capacity` while GPU groups pass | the cloud(s) you target don't have CPU capacity right now | by default we already try SECURE then COMMUNITY. If both are full, add DC-pinned candidates: `CPU_CANDIDATES="cpu-secure:SECURE,cpu-community:COMMUNITY,cpu-eu:COMMUNITY:EU-RO-1+EU-NL-1,cpu-us:COMMUNITY:US-OR-1"` |
| pod stays in `ssh endpoint not assigned yet` past `STALL_HINT_AFTER` | slow image pull or Docker Hub `toomanyrequests` | add registry auth, reduce `MAX_PARALLEL`, or wait 6 h for the Hub rate limit to reset |
| `ssh_probe=FAIL — Permission denied (publickey)` | wrong SSH key | export `RUNPOD_SSH_KEY=/path/to/private/key` whose public half is on the Runpod account |
| `pod entered TIMEOUT state` repeatedly on Blackwell GPUs for a `pytorch` group | PyTorch ≤ 2.6 has no `sm_100`/`sm_120` kernels | add `exclude_instances: ["*Blackwell*"]` to the group |
| `nvidia-container-cli: requirement error: unsatisfied condition: cuda>=X.Y` in pod logs | image needs a newer driver than the host has | set `min_cuda_version: "X.Y"` in the manifest (only needed for tags without a `cuXYZW`/`cudaXYZW` marker) |
| `jupyter check (in-pod) FAILED -- start.sh did not bring up JupyterLab` | `start.sh` is launching Jupyter with the wrong Python interpreter (classic Ubuntu 22.04 `python3` → 3.10 vs `python` → 3.12) | fix `container-template/start.sh` to use `python -m jupyter lab` |
| `jupyter check (public proxy) FAILED` but in-pod check passed | port exposed as `8888/tcp` instead of `8888/http`, OR proxy hasn't registered the pod yet | check `pod create --ports` arg; bump `JUPYTER_PROXY_TIMEOUT` if proxy is just slow |
| script hangs at `Cleaning up N leftover pod(s)…` | Runpod API is slow to respond to delete | wait it out; `--terminate-after` (~2 h) is the backstop and will kill anything we missed |


## Exit code

`0` only when every image PASSed, OR when only SKIPs happened and
`ON_SKIP ∈ {warn, pass}`. `1` if any image FAILed (broken container —
always fatal), or if any image SKIPped under the default `ON_SKIP=fail`.

SKIPs mean the smoke test never actually ran on the image (Runpod had no
capacity on every candidate, or every candidate landed on a stuck host)
— that's effectively zero validation, so the default is strict.
Override with:

* `ON_SKIP=warn` to keep the job green but get a GitHub Actions warning
  annotation in the PR check tab (visible signal without blocking the PR).
* `ON_SKIP=pass` to fully suppress the signal (no annotation at all).

Unknown values silently coerce to `fail`.
