#!/usr/bin/env python3
"""Generate a test manifest YAML for tests/test_images.py.

Reads a JSON array of image refs from the image-name action and emits the
minimal manifest format consumed by the local Runpod smoke-test runner.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

GPU_TAG_PATTERN = re.compile(r"(cuda|pytorch|py|rocm)", re.IGNORECASE)


def is_gpu_ref(ref: str) -> bool:
    """A ref is GPU iff its tag contains one of our GPU markers."""
    _, _, tag = ref.rpartition(":")
    return bool(GPU_TAG_PATTERN.search(tag))


def render_yaml(groups: dict) -> str:
    """Hand-rolled YAML emitter for the runner's minimal parser."""
    lines: list[str] = []
    for grp_name, body in groups.items():
        lines.append(f"{grp_name}:")
        lines.append("    images:")
        for img in body["images"]:
            lines.append(f"    - {img}")
        if "instances" in body:
            lines.append("    instances:")
            for inst in body["instances"]:
                lines.append(f"    - {inst}")
        for key in (
            "max_price_per_hour",
            "check_all_gpu",
            "min_vram_gb",
            "manufacturer",
            "min_cuda_version",
            "test_jupyter",
        ):
            if key in body:
                val = body[key]
                if isinstance(val, bool):
                    val = "true" if val else "false"
                lines.append(f"    {key}: {val}")
        if body.get("test_ports"):
            lines.append("    test_ports:")
            for port in body["test_ports"]:
                lines.append(f"    - {port}")
        if body.get("exclude_instances"):
            lines.append("    exclude_instances:")
            for pat in body["exclude_instances"]:
                lines.append(f'    - "{pat}"')
    return "\n".join(lines) + "\n"


def build_groups(
    profile: str,
    refs: list[str],
    *,
    budget: float,
    min_vram_gb: int,
    manufacturer: str,
    test_jupyter: bool = False,
    check_all_gpu: bool = False,
    test_ports: list[int] | None = None,
    exclude_instances: list[str] | None = None,
    min_cuda_version: str | None = None,
) -> dict:
    exclude_instances = list(exclude_instances or [])
    test_ports = list(test_ports or [])

    def _decorate(body: dict, *, gpu_group: bool) -> dict:
        if gpu_group:
            if check_all_gpu:
                body["check_all_gpu"] = True
            else:
                body["max_price_per_hour"] = budget
            body["min_vram_gb"] = min_vram_gb
            body["manufacturer"] = manufacturer
        if test_jupyter:
            body["test_jupyter"] = True
        if test_ports:
            body["test_ports"] = list(test_ports)
        if exclude_instances:
            body["exclude_instances"] = list(exclude_instances)
        if min_cuda_version:
            body["min_cuda_version"] = min_cuda_version
        return body

    if profile == "base":
        cpu = [r for r in refs if not is_gpu_ref(r)]
        gpu = [r for r in refs if is_gpu_ref(r)]
        groups: dict = {}
        if cpu:
            groups["base_cpu"] = _decorate({"images": cpu}, gpu_group=False)
        if gpu:
            groups["base_gpu"] = _decorate({"images": gpu}, gpu_group=True)
        return groups

    if profile == "gpu":
        return {
            "base_gpu": _decorate({"images": refs}, gpu_group=True)
        }

    raise ValueError(f"unknown profile: {profile!r}")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--profile", required=True, choices=["base", "gpu"])
    ap.add_argument(
        "--refs",
        required=True,
        help="JSON array of image refs (output of .github/actions/image-name)",
    )
    ap.add_argument("--budget", type=float, default=1.0)
    ap.add_argument("--min-vram-gb", type=int, default=16)
    ap.add_argument("--manufacturer", default="Nvidia")
    ap.add_argument("--test-jupyter", action="store_true")
    ap.add_argument("--check-all-gpu", action="store_true")
    ap.add_argument(
        "--test-port",
        action="append",
        default=[],
        type=int,
        metavar="PORT",
        help="HTTP port to expose and probe. Repeat for multiple ports.",
    )
    ap.add_argument(
        "--exclude-instance",
        action="append",
        default=[],
        metavar="PATTERN",
    )
    ap.add_argument("--min-cuda-version", default="", metavar="X.Y")
    ap.add_argument("--output", required=True, type=Path)
    args = ap.parse_args()

    try:
        refs = json.loads(args.refs)
    except json.JSONDecodeError as exc:
        print(f"--refs is not valid JSON: {exc}", file=sys.stderr)
        return 1

    if not isinstance(refs, list) or not refs:
        print("--refs must be a non-empty JSON array", file=sys.stderr)
        return 1

    groups = build_groups(
        args.profile,
        refs,
        budget=args.budget,
        min_vram_gb=args.min_vram_gb,
        manufacturer=args.manufacturer,
        test_jupyter=args.test_jupyter,
        check_all_gpu=args.check_all_gpu,
        test_ports=args.test_port,
        exclude_instances=args.exclude_instance,
        min_cuda_version=(args.min_cuda_version or None),
    )

    if not groups:
        print(
            f"No groups produced from {len(refs)} refs with profile "
            f"{args.profile!r}",
            file=sys.stderr,
        )
        return 1

    args.output.parent.mkdir(parents=True, exist_ok=True)
    body = render_yaml(groups)
    args.output.write_text(body)
    print(f"Wrote {args.output}:")
    print(body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
