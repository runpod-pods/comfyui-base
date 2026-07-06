#!/usr/bin/env python3
"""Smoke-test Runpod container images.

Entry point. See ./README.md for the manifest schema, env vars, and how
the CUDA / Jupyter checks work. All implementation lives in the
runpod_smoke/ package next to this file.

Usage:
    ./test_images.py [path/to/images.yaml] [group_filter]

Requirements: runpodctl (logged in), python3 >= 3.9
"""

from __future__ import annotations

import os
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

# When invoked via a symlink (we keep one at /tmp/runpod-scripts/testing/),
# Python puts the symlink's directory on sys.path, NOT the directory that
# actually contains the `runpod_smoke/` package. Resolve the real path so
# our package imports work regardless of how the script was launched.
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

from runpod_smoke import config
from runpod_smoke.instances import (
    discover_gpu_catalog,
    discover_gpu_id_map,
    is_known_gpu,
    resolve_instances,
)
from runpod_smoke.log import ensure_worker_tag, log
from runpod_smoke.manifest import (
    _normalize_bool,
    _normalize_cuda_version,
    parse_manifest,
)
from runpod_smoke.pod import discover_registry_auth
from runpod_smoke.runpodctl import runpodctl
from runpod_smoke.runner import test_image


# Each entry: (image, group, instances-to-try). One pod is created per
# entry; the runner iterates instances internally until something settles.
#
# For `check_all_gpu: true` groups, `_build_jobs` emits N jobs per image
# (one per resolved instance) each with a single-element `instances`
# list, so `test_image` doesn't iterate and we get a per-(image, instance)
# row in the summary.
Job = tuple[str, str, list[str]]

# Per-attempt outcome: (image, status, note, instance_used). Self-
# contained so the summary can dump every row directly, including
# `check_all_gpu` runs where one image produces many rows. `status` is
# one of "PASS" / "FAIL" / "SKIP". `instance_used` is "" when the test
# never landed on any host (all UNAVAILABLE / STUCK).
Result = tuple[str, str, str, str]


# ---------------------------------------------------------------------------
# CLI / preflight
# ---------------------------------------------------------------------------


def _parse_args() -> tuple[Path, Optional[str]]:
    """argv → (manifest_path, optional group filter). No validation here
    — that's `_check_prereqs`'s job."""
    images_path = Path(sys.argv[1] if len(sys.argv) > 1 else "images")
    group_filter = sys.argv[2] if len(sys.argv) > 2 else None
    return images_path, group_filter


def _check_prereqs(manifest_path: Path) -> Optional[int]:
    """Return None on success, or an exit-code int on failure. Verifies:
      1. the manifest file actually exists
      2. `runpodctl user` succeeds (the CLI has a valid API key)
    Anything else (GPU catalog, registry auth) is best-effort — the script
    degrades gracefully if those are missing."""
    if not manifest_path.is_file():
        log(f"Images manifest not found: {manifest_path}")
        return 1
    auth = runpodctl("user", timeout=15)
    if auth.returncode != 0:
        log("runpodctl is not authenticated. Run 'runpodctl doctor'.")
        return 1
    return None


# ---------------------------------------------------------------------------
# Runtime-state initialization (GPU map + catalog + registry auth)
# ---------------------------------------------------------------------------


def _init_gpu_catalog() -> None:
    config.GPU_ID_MAP.update(discover_gpu_id_map())
    log(f"discovered {len(config.GPU_ID_MAP)} GPU types from runpodctl")

    config.GPU_CATALOG.extend(discover_gpu_catalog())
    if config.GPU_CATALOG:
        log(
            f"loaded GPU pricing for {len(config.GPU_CATALOG)} types "
            "(GraphQL: gpuTypes)"
        )
    else:
        log(
            "warn: no GPU pricing data — budget-based instance selection "
            "(max_price_per_hour) will be disabled. Set RUNPOD_API_KEY or "
            "ensure ~/.runpod/config.toml has 'apikey'."
        )


def _init_registry_auth() -> None:
    if not config.REGISTRY_AUTH_ID:
        config.REGISTRY_AUTH_ID = (
            discover_registry_auth(config.REGISTRY_AUTH_NAME) or ""
        )
    if config.REGISTRY_AUTH_ID:
        log(f"using registry auth: {config.REGISTRY_AUTH_ID}")
    else:
        log(
            "warn: no registry auth configured — Docker Hub pulls will be "
            "anonymous and likely hit the toomanyrequests rate limit"
        )


# ---------------------------------------------------------------------------
# Manifest derivatives (per-group lookup dicts) + instance resolution
# ---------------------------------------------------------------------------


def _coerce_ports(raw_ports: object, group: str) -> list[int]:
    """Best-effort coerce `test_ports` manifest entries to ints. The
    minimal manifest parser keeps list items as strings, so '8188' lands
    here as a str. Anything that doesn't parse is logged + skipped, not
    fatal — a typo in one port shouldn't kill the whole group."""
    if not isinstance(raw_ports, list):
        return []
    ports: list[int] = []
    for entry in raw_ports:
        try:
            ports.append(int(str(entry).strip()))
        except (TypeError, ValueError):
            log(
                f"warn: group '{group}': test_ports entry {entry!r} "
                "is not a valid TCP port — skipping"
            )
    return ports


def _apply_manifest_overrides(manifest: dict[str, dict]) -> None:
    """Populate the per-group dicts on `config` that `pod.create_pod` and
    `runner.test_pair` consult at run-time:
      * GROUP_MIN_CUDA   — fallback CUDA for tag-less images (NGC etc.)
      * GROUP_TEST_JUPYTER — opt-in for Jupyter-API-specific probes
      * GROUP_TEST_PORTS  — generic per-port HTTP probes (in-pod + proxy)
      * GROUP_CHECK_ALL_GPU — disable PASS short-circuit, test on every
        resolved instance (paired with `_select_all_gpus` in instances.py)
    """
    for grp, contents in manifest.items():
        normalized = _normalize_cuda_version(contents.get("min_cuda_version"))
        if normalized:
            config.GROUP_MIN_CUDA[grp] = normalized
            log(
                f"group '{grp}': min_cuda_version={normalized} "
                "(applied when image tag has no embedded CUDA)"
            )
    for grp, contents in manifest.items():
        if _normalize_bool(contents.get("test_jupyter")):
            config.GROUP_TEST_JUPYTER[grp] = True
            # JUPYTER_TEST_PASSWORD is a hard-coded throw-away token for
            # short-lived test pods (see config.py), but we still redact
            # it in logs so the pattern stays clean for CodeQL and any
            # future operator who copy-pastes the log into a ticket.
            log(
                f"group '{grp}': test_jupyter=true "
                "(JUPYTER_PASSWORD=<redacted>, expose 8888/http)"
            )
    for grp, contents in manifest.items():
        ports = _coerce_ports(contents.get("test_ports"), grp)
        if ports:
            config.GROUP_TEST_PORTS[grp] = ports
            log(
                f"group '{grp}': test_ports={ports} "
                "(expose each as <port>/http, probe in-pod + via proxy)"
            )
    for grp, contents in manifest.items():
        if _normalize_bool(contents.get("check_all_gpu")):
            config.GROUP_CHECK_ALL_GPU[grp] = True
            log(
                f"group '{grp}': check_all_gpu=true "
                "(test on EVERY resolved instance; no PASS short-circuit)"
            )


def _resolve_all_instances(manifest: dict[str, dict]) -> dict[str, list[str]]:
    """Per-group instance lookup — done up-front so we can warn about
    typos / empty lists once, instead of per-job."""
    return {grp: resolve_instances(grp, contents)
            for grp, contents in manifest.items()}


def _warn_unknown_instances(resolved: dict[str, list[str]]) -> None:
    """Surface manifest entries that don't map to any Runpod display name
    (typos / casing) so the user sees them once at startup instead of
    inside the per-job logs."""
    unmapped = sorted({
        inst
        for instances in resolved.values()
        for inst in instances
        if not config.is_cpu_instance(inst) and not is_known_gpu(inst)
    })
    if not unmapped:
        return
    log(
        f"warn: {len(unmapped)} instance(s) don't match any Runpod "
        "displayName — check spelling/casing:"
    )
    for inst in unmapped:
        log(f"  - {inst!r}", indent=1)


def _log_budget_picks(
    manifest: dict[str, dict],
    resolved: dict[str, list[str]],
) -> None:
    """For each budget-mode group, log what the catalog filter picked.
    Particularly useful when the user wrote `max_price_per_hour: X` and
    wants to see which GPUs cleared the threshold."""
    for grp, instances in resolved.items():
        contents = manifest[grp]
        if "max_price_per_hour" not in contents or contents.get("instances"):
            continue
        budget = contents["max_price_per_hour"]
        preview = ", ".join(instances[:8]) + (
            f", ... (+{len(instances) - 8} more)" if len(instances) > 8 else ""
        )
        log(
            f"group '{grp}': budget ≤ ${budget}/hr → {len(instances)} "
            f"candidate(s): {preview or '(none — no GPU fits)'}"
        )


# ---------------------------------------------------------------------------
# Job fan-out
# ---------------------------------------------------------------------------


def _build_jobs(
    manifest: dict[str, dict],
    resolved: dict[str, list[str]],
    group_filter: Optional[str],
    results: list[Result],
) -> list[Job]:
    """Flatten the manifest into a list of `(image, group, instances)`
    jobs that can run independently. Groups with no resolvable instances
    are recorded directly into `results` as SKIPs (caller handles the
    summary print).

    For groups with `check_all_gpu: true` we emit ONE job per (image,
    instance) — each with a single-element instances list — so the
    runner doesn't iterate and we surface a per-instance row in the
    summary. Default groups stay as one job per image with the full
    candidate list (runner short-circuits on PASS).
    """
    jobs: list[Job] = []
    for group, contents in manifest.items():
        if group_filter and group != group_filter:
            continue
        instances = resolved.get(group, [])
        if not instances:
            log(
                f"skipping group '{group}': no instances resolved "
                "(none of 'instances:', 'max_price_per_hour:' or "
                "'check_all_gpu:' produced any candidates)"
            )
            for img in contents.get("images", []):
                results.append((img, "SKIP", "no instances configured", ""))
            continue
        check_all = config.GROUP_CHECK_ALL_GPU.get(group, False)
        for img in contents.get("images", []):
            if check_all:
                for inst in instances:
                    jobs.append((img, group, [inst]))
            else:
                jobs.append((img, group, instances))
    return jobs


# ---------------------------------------------------------------------------
# Job execution
# ---------------------------------------------------------------------------


def _run_jobs_serial(jobs: list[Job], results: list[Result]) -> None:
    """Single-threaded run — no worker tags, simpler logs, group-header
    banner each time the group changes."""
    current_group: Optional[str] = None
    for img, group, instances in jobs:
        if group != current_group:
            print()
            log(f"---------- group: {group} ----------")
            current_group = group
        status, note, inst = test_image(img, instances, group)
        results.append((img, status, note, inst))


def _run_one_tagged_job(job: Job) -> Result:
    """ThreadPool worker. The W<N> tag is assigned to the THREAD (not the
    job), so e.g. with 5 jobs and 3 workers you still see only W1/W2/W3,
    each handling 1-2 jobs sequentially."""
    img, grp, insts = job
    ensure_worker_tag()
    log(f"start [group={grp}] image={img}")
    status, note, inst = test_image(img, insts, grp)
    log(f"done  [group={grp}] image={img} -> {status}")
    return (img, status, note, inst)


def _run_jobs_parallel(jobs: list[Job], results: list[Result]) -> None:
    """ThreadPool fan-out capped at MAX_PARALLEL. Each worker holds at
    most one pod at a time."""
    with ThreadPoolExecutor(max_workers=config.MAX_PARALLEL) as pool:
        futures = [pool.submit(_run_one_tagged_job, job) for job in jobs]
        for fut in as_completed(futures):
            results.append(fut.result())


def _run_jobs(jobs: list[Job], results: list[Result]) -> None:
    if not jobs:
        log("no jobs to run after filtering")
        return
    print()
    log(
        f"==================== running {len(jobs)} job(s) "
        f"with MAX_PARALLEL={config.MAX_PARALLEL} ===================="
    )
    if config.MAX_PARALLEL <= 1:
        _run_jobs_serial(jobs, results)
    else:
        _run_jobs_parallel(jobs, results)


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


def _format_result_line(want: str, img: str, status: str, note: str,
                        instance: str) -> Optional[str]:
    """Format one row of the summary, or None when this result doesn't
    belong in the `want` bucket. CPU labels ('cpu-secure', 'cpu-community',
    …) are already human-readable, so they go to the summary verbatim."""
    if status != want:
        return None
    inst_str = f" [{instance}]" if instance else ""
    note_str = f" -- {note}" if note else ""
    return f"  {want:6s} {img}{inst_str}{note_str}"


def _print_summary(results: list[Result]) -> int:
    """Print the SUMMARY block and return the exit code.

    FAIL is ALWAYS fatal (exit 1) — a broken container is never something
    we want to slip past CI.

    For SKIPs (test never actually ran against the image) the behaviour
    is driven by `config.ON_SKIP`:
      'fail' (default) → exit 1 + `::error::` GitHub annotation
      'warn'           → exit 0 + `::warning::` GitHub annotation
                         (job stays green but the run shows a yellow
                         warning bubble in the PR check tab — useful when
                         capacity-shortage shouldn't block PRs but you
                         still want a visible signal)
      'pass'           → exit 0, no annotation (legacy lenient mode)

    A run with BOTH FAIL and SKIP exits 1 with the FAIL annotation; the
    SKIP count is already visible in the totals line so we don't double-
    annotate.

    `check_all_gpu` runs produce multiple rows per image — one per
    instance — so the totals reflect attempt count, not image count.
    """
    print()
    print("=" * 84)
    print(" SUMMARY ".center(84, "="))
    print("=" * 84)
    counts: dict[str, int] = defaultdict(int)
    for _img, status, _note, _inst in results:
        counts[status] += 1
    print(
        f"totals: {counts['PASS']} PASS, "
        f"{counts['FAIL']} FAIL, "
        f"{counts['SKIP']} SKIP\n"
    )
    for want in ("FAIL", "SKIP", "PASS"):
        for img, status, note, instance in results:
            line = _format_result_line(want, img, status, note, instance)
            if line is not None:
                print(line)

    if counts["FAIL"] > 0:
        return 1
    if counts["SKIP"] == 0 or config.ON_SKIP == "pass":
        return 0

    # SKIPs happened and the operator wants to be told. Annotate +
    # decide exit code based on the mode.
    msg = (
        f"{counts['SKIP']} image(s) SKIPped — no real validation "
        "happened. Runpod had no capacity on every candidate instance "
        "type, or every candidate landed on a stuck host. "
        "Set ON_SKIP=pass to silence this, ON_SKIP=warn to keep the "
        "job green with a warning, or ON_SKIP=fail (default) to make "
        "it fatal."
    )
    print()
    if config.ON_SKIP == "warn":
        print(f"::warning::{msg}")
        return 0
    # 'fail' — also the safe default for any unknown value (coerced
    # in config._coerce_on_skip).
    print(f"::error::{msg}")
    return 1


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    manifest_path, group_filter = _parse_args()

    rc = _check_prereqs(manifest_path)
    if rc is not None:
        return rc

    _init_gpu_catalog()
    _init_registry_auth()

    manifest = parse_manifest(manifest_path)
    _apply_manifest_overrides(manifest)

    resolved = _resolve_all_instances(manifest)
    _warn_unknown_instances(resolved)
    _log_budget_picks(manifest, resolved)

    results: list[Result] = []
    jobs = _build_jobs(manifest, resolved, group_filter, results)
    _run_jobs(jobs, results)

    return _print_summary(results)


if __name__ == "__main__":
    sys.exit(main())
