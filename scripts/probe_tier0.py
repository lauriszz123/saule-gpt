#!/usr/bin/env python3
"""Tier 0 probe: prove the RX 6700 XT actually computes, and record the stack.

Run this ON THE LINUX SIDE of the training machine. It is the one piece of
Stage 0 that cannot be produced from any other host, because it measures the
ROCm stack as installed.

    python3 scripts/probe_tier0.py --json probe_tier0.json

It answers, with measurements rather than assertions:

  * distro / kernel / amdgpu driver / ROCm version, together, because a
    mismatch between driver and ROCm is the usual cause of a card that is
    detected and then does not compute;
  * whether the invoking user is in the `render` and `video` groups;
  * whether `torch.cuda.is_available()` is True AND the device genuinely
    executes -- a matmul is checked numerically against the CPU result and
    timed, so a silent CPU fallback shows up as a wrong device name or as
    TFLOPs in the CPU's range rather than the card's;
  * achievable fp32 / fp16 TFLOPs, and whether bf16 is real or emulated;
  * which `scaled_dot_product_attention` backend wins, since flash-attn does
    not support RDNA2;
  * whether fp16 autocast + GradScaler completes a step without overflow;
  * VRAM, RAM, free disk and the HSA_OVERRIDE_GFX_VERSION setting.

Exit status is 0 if the GPU proved itself, 1 if it did not, 2 if the probe
could not run at all. Nothing here trains anything or writes to the dataset
tree; it allocates, multiplies and frees.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Callable

# A matmul large enough that kernel-launch overhead is noise, small enough to
# fit a 12 GB card three times over (three fp32 4096x4096 matrices = 192 MB).
MATMUL_N = 4096
MATMUL_WARMUP = 3
MATMUL_ITERS = 20

# Below this, a "GPU" result is almost certainly the CPU wearing a GPU's name.
# A Ryzen 5 7600 peaks near 0.5 fp32 TFLOPs; the RX 6700 XT's fp32 peak is
# about 13 TFLOPs, so 2.0 separates them with room for a bad MFU.
CPU_FALLBACK_TFLOPS_CEILING = 2.0


@dataclass
class Probe:
    """Everything the probe learned. Serialised verbatim into the report."""

    ok: bool = False
    failures: list[str] = field(default_factory=list)
    host: dict[str, Any] = field(default_factory=dict)
    rocm: dict[str, Any] = field(default_factory=dict)
    torch: dict[str, Any] = field(default_factory=dict)
    compute: dict[str, Any] = field(default_factory=dict)
    attention: dict[str, Any] = field(default_factory=dict)
    precision: dict[str, Any] = field(default_factory=dict)

    def fail(self, msg: str) -> None:
        self.failures.append(msg)


def sh(cmd: list[str], timeout: float = 20.0) -> str | None:
    """Run a command, returning stripped stdout, or None if it is unusable.

    Every external tool here is optional -- a missing `rocminfo` is a finding,
    not a crash -- so failure returns None rather than raising.
    """
    if shutil.which(cmd[0]) is None:
        return None
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return f"<{type(exc).__name__}: {exc}>"
    text = (out.stdout or out.stderr).strip()
    return text or None


def read_file(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read().strip()
    except OSError:
        return None


def probe_host(p: Probe) -> None:
    p.host["platform"] = platform.platform()
    p.host["kernel"] = platform.release()
    p.host["machine"] = platform.machine()
    p.host["python"] = sys.version
    p.host["python_executable"] = sys.executable

    os_release = read_file("/etc/os-release") or ""
    fields = dict(
        line.split("=", 1)
        for line in os_release.splitlines()
        if "=" in line and not line.startswith("#")
    )
    p.host["os_release"] = {k: v.strip('"') for k, v in fields.items()}
    # elementary OS reports ID=elementary, which is what makes AMD's
    # amdgpu-install script baulk; UBUNTU_CODENAME is the field that says
    # which ROCm repo to register.
    p.host["ubuntu_codename"] = p.host["os_release"].get("UBUNTU_CODENAME")

    p.host["cpu_model"] = next(
        (
            line.split(":", 1)[1].strip()
            for line in (read_file("/proc/cpuinfo") or "").splitlines()
            if line.startswith("model name")
        ),
        None,
    )
    p.host["cpu_count"] = os.cpu_count()

    meminfo = read_file("/proc/meminfo") or ""
    for key in ("MemTotal", "MemAvailable"):
        line = next((l for l in meminfo.splitlines() if l.startswith(key)), None)
        if line:
            kb = int(line.split()[1])
            p.host[f"{key.lower()}_gb"] = round(kb / 1024 / 1024, 2)

    usage = shutil.disk_usage(os.getcwd())
    p.host["cwd_disk_free_gb"] = round(usage.free / 1024**3, 2)
    p.host["cwd_disk_total_gb"] = round(usage.total / 1024**3, 2)

    # Group membership: without render+video the card is visible but unusable.
    groups = sh(["id", "-nG"]) or ""
    p.host["groups"] = groups.split()
    for required in ("render", "video"):
        if required not in p.host["groups"]:
            p.fail(
                f"user is not in the `{required}` group -- "
                f"run `sudo usermod -aG {required} $USER`, then log out and back in"
            )

    p.host["in_container"] = os.path.exists("/.dockerenv")
    p.host["HSA_OVERRIDE_GFX_VERSION"] = os.environ.get("HSA_OVERRIDE_GFX_VERSION")
    p.host["PYTORCH_HIP_ALLOC_CONF"] = os.environ.get("PYTORCH_HIP_ALLOC_CONF")
    p.host["HIP_VISIBLE_DEVICES"] = os.environ.get("HIP_VISIBLE_DEVICES")


def probe_rocm(p: Probe) -> None:
    p.rocm["amdgpu_driver_version"] = read_file(
        "/sys/module/amdgpu/version"
    ) or "<not loaded>"
    p.rocm["rocm_version_file"] = read_file("/opt/rocm/.info/version")
    p.rocm["dkms_status"] = sh(["dkms", "status"])

    info = sh(["rocminfo"], timeout=60.0)
    p.rocm["rocminfo_present"] = info is not None
    if info:
        # gfx targets, in order. gfx1031 is the RX 6700 XT; the override makes
        # it run the gfx1030 kernel set.
        p.rocm["gfx_targets"] = sorted(
            {
                tok
                for line in info.splitlines()
                if "gfx" in line
                for tok in line.split()
                if tok.startswith("gfx")
            }
        )
        p.rocm["agents"] = [
            line.split(":", 1)[1].strip()
            for line in info.splitlines()
            if line.strip().startswith("Marketing Name")
        ]
    else:
        p.fail("`rocminfo` not found or failed -- the ROCm userspace is not installed")

    smi = sh(["rocm-smi", "--showproductname", "--showmeminfo", "vram"], timeout=60.0)
    p.rocm["rocm_smi"] = smi

    for dev in ("/dev/kfd", "/dev/dri"):
        p.rocm[f"{dev}_present"] = os.path.exists(dev)
        if not os.path.exists(dev):
            p.fail(f"{dev} is missing -- the amdgpu kernel driver is not providing it")


def probe_torch(p: Probe) -> "Any | None":
    try:
        import torch
    except Exception as exc:  # noqa: BLE001 - any import failure is a finding
        p.torch["import_error"] = f"{type(exc).__name__}: {exc}"
        p.fail(f"could not import torch: {exc}")
        return None

    p.torch["version"] = torch.__version__
    p.torch["hip_version"] = getattr(torch.version, "hip", None)
    p.torch["cuda_version"] = getattr(torch.version, "cuda", None)
    p.torch["is_available"] = torch.cuda.is_available()
    p.torch["device_count"] = torch.cuda.device_count() if torch.cuda.is_available() else 0

    if not torch.version.hip:
        p.fail(
            "this torch wheel is not a ROCm build "
            f"(torch.version.hip is {torch.version.hip!r}) -- install from the "
            "ROCm wheel index, see docs/RUNBOOK.md"
        )
    if not torch.cuda.is_available():
        p.fail("torch.cuda.is_available() is False -- no usable device")
        return torch

    props = torch.cuda.get_device_properties(0)
    p.torch["device_name"] = props.name
    p.torch["gcn_arch"] = getattr(props, "gcnArchName", None)
    p.torch["vram_gb"] = round(props.total_memory / 1024**3, 2)
    p.torch["multi_processor_count"] = props.multi_processor_count
    return torch


def _time_matmul(torch: Any, device: str, dtype: Any) -> tuple[float, float]:
    """Return (seconds per matmul, achieved TFLOPs) for one N*N*N product."""
    n = MATMUL_N
    a = torch.randn(n, n, device=device, dtype=dtype)
    b = torch.randn(n, n, device=device, dtype=dtype)

    for _ in range(MATMUL_WARMUP):
        a @ b
    if device != "cpu":
        torch.cuda.synchronize()

    start = time.perf_counter()
    for _ in range(MATMUL_ITERS):
        a @ b
    if device != "cpu":
        torch.cuda.synchronize()
    elapsed = (time.perf_counter() - start) / MATMUL_ITERS

    del a, b
    if device != "cpu":
        torch.cuda.empty_cache()
    # 2 FLOP per multiply-accumulate.
    return elapsed, (2 * n**3) / elapsed / 1e12


def probe_compute(p: Probe, torch: Any) -> None:
    """The load-bearing check: does the device compute, and is it right?"""
    n = 1024
    gen = torch.Generator().manual_seed(0)
    a_cpu = torch.randn(n, n, generator=gen, dtype=torch.float32)
    b_cpu = torch.randn(n, n, generator=gen, dtype=torch.float32)
    expected = a_cpu @ b_cpu

    try:
        a_gpu = a_cpu.cuda()
        b_gpu = b_cpu.cuda()
        got = (a_gpu @ b_gpu).cpu()
    except Exception as exc:  # noqa: BLE001
        p.compute["error"] = f"{type(exc).__name__}: {exc}"
        p.fail(f"a matmul on the device raised {type(exc).__name__}: {exc}")
        return

    # fp32 accumulation over 1024 terms: 1e-3 absolute is loose enough for
    # any legitimate kernel and tight enough to catch a garbage result.
    max_abs_err = (got - expected).abs().max().item()
    p.compute["matmul_max_abs_err_vs_cpu"] = max_abs_err
    p.compute["numerically_correct"] = bool(max_abs_err < 1e-3)
    if not p.compute["numerically_correct"]:
        p.fail(
            f"device matmul disagrees with the CPU by {max_abs_err:.3e} -- "
            "the kernels are producing wrong results, do not train on this"
        )

    p.compute["device_of_result"] = str((a_gpu @ b_gpu).device)
    del a_gpu, b_gpu
    torch.cuda.empty_cache()

    for label, dtype in (("fp32", torch.float32), ("fp16", torch.float16)):
        try:
            secs, tflops = _time_matmul(torch, "cuda", dtype)
            p.compute[f"{label}_tflops"] = round(tflops, 2)
            p.compute[f"{label}_secs_per_matmul"] = round(secs, 6)
        except Exception as exc:  # noqa: BLE001
            p.compute[f"{label}_error"] = f"{type(exc).__name__}: {exc}"

    try:
        cpu_secs, cpu_tflops = _time_matmul(torch, "cpu", torch.float32)
        p.compute["cpu_fp32_tflops"] = round(cpu_tflops, 3)
        p.compute["cpu_fp32_secs_per_matmul"] = round(cpu_secs, 6)
    except Exception as exc:  # noqa: BLE001
        p.compute["cpu_error"] = f"{type(exc).__name__}: {exc}"

    gpu_fp32 = p.compute.get("fp32_tflops")
    if gpu_fp32 is not None and gpu_fp32 < CPU_FALLBACK_TFLOPS_CEILING:
        p.fail(
            f"fp32 matmul reached only {gpu_fp32} TFLOPs, below the "
            f"{CPU_FALLBACK_TFLOPS_CEILING} TFLOPs floor this card should clear -- "
            "this is consistent with a silent fall back to the CPU"
        )

    # bf16: RDNA2 has no fast path, so this is expected to be markedly slower
    # than fp16. Measured rather than assumed, because the number decides the
    # precision default.
    try:
        _, bf16_tflops = _time_matmul(torch, "cuda", torch.bfloat16)
        p.precision["bf16_tflops"] = round(bf16_tflops, 2)
        fp16 = p.compute.get("fp16_tflops")
        if fp16:
            ratio = bf16_tflops / fp16
            p.precision["bf16_over_fp16_ratio"] = round(ratio, 3)
            p.precision["bf16_likely_emulated"] = bool(ratio < 0.6)
    except Exception as exc:  # noqa: BLE001
        p.precision["bf16_error"] = f"{type(exc).__name__}: {exc}"


def probe_amp(p: Probe, torch: Any) -> None:
    """One fp16 autocast + GradScaler step, to catch overflow at the source."""
    try:
        model = torch.nn.Sequential(
            torch.nn.Linear(512, 2048),
            torch.nn.GELU(),
            torch.nn.Linear(2048, 512),
        ).cuda()
        opt = torch.optim.AdamW(model.parameters(), lr=1e-4)
        scaler = torch.cuda.amp.GradScaler()
        x = torch.randn(64, 512, device="cuda")
        target = torch.randn(64, 512, device="cuda")

        with torch.autocast(device_type="cuda", dtype=torch.float16):
            loss = torch.nn.functional.mse_loss(model(x), target)
        scaler.scale(loss).backward()
        scaler.unscale_(opt)
        grad_norm = torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
        scaler.step(opt)
        scaler.update()

        p.precision["amp_fp16_step_ok"] = True
        p.precision["amp_fp16_loss"] = float(loss.item())
        p.precision["amp_fp16_grad_norm"] = float(grad_norm)
        p.precision["amp_fp16_grad_finite"] = bool(torch.isfinite(grad_norm))
        p.precision["amp_fp16_scale"] = float(scaler.get_scale())
        if not torch.isfinite(grad_norm):
            p.fail("fp16 autocast produced a non-finite gradient norm on step 1")
    except Exception as exc:  # noqa: BLE001
        p.precision["amp_fp16_step_ok"] = False
        p.precision["amp_fp16_error"] = f"{type(exc).__name__}: {exc}"
        p.fail(f"fp16 autocast step failed: {type(exc).__name__}: {exc}")


def probe_attention(p: Probe, torch: Any) -> None:
    """Which SDPA backend wins on RDNA2. flash-attn does not support it."""
    from torch.nn.attention import SDPBackend, sdpa_kernel

    batch, heads, seq, dim = 8, 12, 1024, 64
    q = torch.randn(batch, heads, seq, dim, device="cuda", dtype=torch.float16)
    k = torch.randn_like(q)
    v = torch.randn_like(q)

    backends = {
        "math": SDPBackend.MATH,
        "mem_efficient": SDPBackend.EFFICIENT_ATTENTION,
        "flash": SDPBackend.FLASH_ATTENTION,
    }
    for name, backend in backends.items():
        try:
            with sdpa_kernel(backend):
                for _ in range(3):
                    torch.nn.functional.scaled_dot_product_attention(
                        q, k, v, is_causal=True
                    )
                torch.cuda.synchronize()
                start = time.perf_counter()
                for _ in range(10):
                    torch.nn.functional.scaled_dot_product_attention(
                        q, k, v, is_causal=True
                    )
                torch.cuda.synchronize()
            p.attention[name] = {
                "ok": True,
                "ms_per_call": round((time.perf_counter() - start) / 10 * 1000, 3),
            }
        except Exception as exc:  # noqa: BLE001
            p.attention[name] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    working = {
        name: entry["ms_per_call"]
        for name, entry in p.attention.items()
        if entry.get("ok")
    }
    p.attention["winner"] = min(working, key=working.get) if working else None
    if not working:
        p.fail("no scaled_dot_product_attention backend ran at all")

    del q, k, v
    torch.cuda.empty_cache()


def render(p: Probe) -> str:
    lines: list[str] = []
    add = lines.append

    add("=" * 72)
    add("TIER 0 PROBE -- Ryzen 5 7600 + RX 6700 XT (gfx1031), Linux")
    add("=" * 72)

    add("")
    add("-- host " + "-" * 64)
    for key in (
        "platform",
        "kernel",
        "cpu_model",
        "cpu_count",
        "memtotal_gb",
        "memavailable_gb",
        "cwd_disk_free_gb",
        "ubuntu_codename",
        "in_container",
        "python",
        "HSA_OVERRIDE_GFX_VERSION",
    ):
        if key in p.host:
            add(f"  {key:28} {p.host[key]}")
    add(f"  {'groups':28} {' '.join(p.host.get('groups', []))}")

    add("")
    add("-- rocm " + "-" * 64)
    for key, value in p.rocm.items():
        if key == "rocm_smi":
            continue
        add(f"  {key:28} {value}")

    add("")
    add("-- torch " + "-" * 63)
    for key, value in p.torch.items():
        add(f"  {key:28} {value}")

    add("")
    add("-- compute (the load-bearing check) " + "-" * 36)
    for key, value in p.compute.items():
        add(f"  {key:28} {value}")

    add("")
    add("-- precision " + "-" * 59)
    for key, value in p.precision.items():
        add(f"  {key:28} {value}")

    add("")
    add("-- attention backends " + "-" * 50)
    for key, value in p.attention.items():
        add(f"  {key:28} {value}")

    add("")
    add("=" * 72)
    if p.ok:
        add("RESULT: the GPU computes, and its results match the CPU.")
    else:
        add(f"RESULT: NOT PROVEN -- {len(p.failures)} finding(s):")
        for failure in p.failures:
            add(f"  * {failure}")
    add("=" * 72)
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", metavar="PATH", help="also write the raw probe here")
    parser.add_argument(
        "--skip-attention",
        action="store_true",
        help="skip the SDPA backend sweep (it allocates ~400 MB)",
    )
    args = parser.parse_args()

    p = Probe()

    if platform.system() != "Linux":
        print(
            f"refusing to run: this probe measures the ROCm stack and this host is "
            f"{platform.system()}. Run it on the Linux side of the training machine.",
            file=sys.stderr,
        )
        return 2

    stages: list[tuple[str, Callable[[], None]]] = [
        ("host", lambda: probe_host(p)),
        ("rocm", lambda: probe_rocm(p)),
    ]
    for name, fn in stages:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            p.fail(f"probe stage `{name}` raised {type(exc).__name__}: {exc}")

    torch = probe_torch(p)
    if torch is not None and p.torch.get("is_available"):
        for name, fn in (
            ("compute", lambda: probe_compute(p, torch)),
            ("amp", lambda: probe_amp(p, torch)),
        ):
            try:
                fn()
            except Exception as exc:  # noqa: BLE001
                p.fail(f"probe stage `{name}` raised {type(exc).__name__}: {exc}")
        if not args.skip_attention:
            try:
                probe_attention(p, torch)
            except Exception as exc:  # noqa: BLE001
                p.fail(f"probe stage `attention` raised {type(exc).__name__}: {exc}")

    p.ok = not p.failures and bool(p.compute.get("numerically_correct"))

    print(render(p))

    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(asdict(p), handle, indent=2, default=str)
        print(f"\nraw probe written to {args.json}")

    return 0 if p.ok else 1


if __name__ == "__main__":
    sys.exit(main())
