# RUNBOOK

Everything needed to rebuild this project from a clean machine. Versions here
are pinned and verified against the package indexes on 2026-10-12; a line that
says "verified" was fetched, not remembered.

> **Status at Stage 0.** The Tier 1a (M1 Air) section is measured. The Tier 0
> (Linux + ROCm) section is **planned and not yet executed** — the Stage 0
> session ran on macOS and could not reach the training machine. Run
> `scripts/probe_tier0.py` on the Linux side and paste its output into
> `docs/DECISIONS.md` before any training is approved.

---

## 0. Machines

| Tier | Machine | Role | Probed at Stage 0 |
|---|---|---|---|
| 0 | Ryzen 5 7600 + RX 6700 XT 12 GB, gfx1031, elementary OS 8 | training + synthesis only | **no** — not reachable from the Stage 0 session |
| 1a | MacBookAir10,1 — Apple M1 (4P+4E), **16 GB** unified, macOS 27.0.1 | demo target, serving | yes |
| 1b | Intel + GeForce GTX 960-class (Maxwell, 2–4 GB VRAM) | required demo target | **no** |
| 2 | ARM single-board computer | the goal | not yet chosen |

Tier 1a's memory was read from the hardware (`hw.memsize` = 17179869184 bytes),
which settles the 8-vs-16 GB question: **16 GB**, so a ~3B base at Q4 is the
right size there with the encoders lazy-loaded.

---

## 1. Tier 0 — Linux training box

### 1.1 Why a container is the recommended route

A native ROCm install touches the same `amdgpu` stack the desktop depends on,
and AMD's `amdgpu-install` script reads `/etc/os-release` and can refuse to run
because elementary OS reports `ID=elementary`. The container route needs only
the kernel driver on the host, and a bad ROCm release can be discarded without
touching the desktop.

Do **not** edit `/etc/os-release`. If the script baulks, read
`UBUNTU_CODENAME` (elementary OS 8 → `noble`) and register the repo by hand.

### 1.2 Host prerequisites

```bash
# the in-tree amdgpu driver is preferred over amdgpu-dkms unless it proves too old
cat /sys/module/amdgpu/version          # driver version
uname -r                                # kernel
grep UBUNTU_CODENAME /etc/os-release    # expect: noble

# the card is unusable without these two groups; log out and back in after
sudo usermod -aG render,video "$USER"
id -nG | tr ' ' '\n' | grep -E '^(render|video)$'

ls -l /dev/kfd /dev/dri                 # both must exist
```

### 1.3 The container

```bash
docker run -it \
  --device=/dev/kfd \
  --device=/dev/dri \
  --group-add video \
  --group-add render \
  --ipc=host \
  --shm-size=16g \
  --cap-add=SYS_PTRACE \
  --security-opt seccomp=unconfined \
  -e HSA_OVERRIDE_GFX_VERSION=10.3.0 \
  -v "$PWD":/workspace \
  -w /workspace \
  rocm/pytorch:latest
```

`HSA_OVERRIDE_GFX_VERSION=10.3.0` is **not optional**: gfx1031 has no official
ROCm support, and the override makes it run the gfx1030 kernel set built for
the RX 6800 and up. Without it PyTorch may see the card and then segfault, or
fall back to the CPU silently — which looks exactly like success until
training is a hundred times slower than it should be.

### 1.4 Native install, if the container is rejected at the gate

Python must be **3.10–3.14**; ROCm wheels exist for `cp310`–`cp314` on the
`rocm6.4` channel (verified). 3.12 is the recommendation — the widest
ecosystem support of the versions that have wheels.

```bash
# register the ROCm repo manually for noble, since the installer may refuse
sudo mkdir -p /etc/apt/keyrings
curl -fsSL https://repo.radeon.com/rocm/rocm.gpg.key \
  | sudo gpg --dearmor -o /etc/apt/keyrings/rocm.gpg
echo 'deb [arch=amd64 signed-by=/etc/apt/keyrings/rocm.gpg] \
https://repo.radeon.com/rocm/apt/6.4 noble main' \
  | sudo tee /etc/apt/sources.list.d/rocm.list
sudo apt update && sudo apt install -y rocm-hip-sdk rocminfo rocm-smi

python3.12 -m venv .venv && source .venv/bin/activate
```

**Verified exact pins** (fetched from `download.pytorch.org` on 2026-10-12):

```bash
pip install \
  --index-url https://download.pytorch.org/whl/rocm6.4 \
  'torch==2.9.1+rocm6.4' \
  'torchvision==0.24.1+rocm6.4' \
  'torchaudio==2.9.1+rocm6.4'
```

| Package | Pin | Wheel tag |
|---|---|---|
| torch | `2.9.1+rocm6.4` | `cp312-cp312-manylinux_2_28_x86_64` |
| torchvision | `0.24.1+rocm6.4` | `cp312-cp312-manylinux_2_28_x86_64` |
| torchaudio | `2.9.1+rocm6.4` | `cp312-cp312-manylinux_2_28_x86_64` |

The `rocm7.0` channel exists and carries torch 2.10.0, but ROCm 7 is the
riskier choice for an unsupported gfx target. Stay on 6.4 until the probe
passes there; revisit only with a measurement.

### 1.5 Environment

```bash
export HSA_OVERRIDE_GFX_VERSION=10.3.0   # mandatory for gfx1031
export PYTORCH_HIP_ALLOC_CONF=expandable_segments:True  # ROCm fragmentation
export HIP_VISIBLE_DEVICES=0
export TOKENIZERS_PARALLELISM=false
```

### 1.6 Prove the GPU computes — the gate

`torch.cuda.is_available()` returning `True` proves nothing on ROCm.

```bash
python3 scripts/probe_tier0.py --json probe_tier0.json
```

Exit status 0 means the device ran a matmul **and** its result matched the CPU
to under 1e-3. Exit 1 means it did not, and the findings list says why. The
probe also reports fp32/fp16 TFLOPs, whether bf16 is emulated, which
`scaled_dot_product_attention` backend wins (flash-attn does not support
RDNA2), and whether one fp16 autocast + `GradScaler` step completes with a
finite gradient norm.

A reported fp32 figure under 2.0 TFLOPs is treated as a failure, because that
is the CPU's range, not this card's.

---

## 2. Tier 1a — M1 Air (measured)

```
MacBookAir10,1 · Apple M1, 8 cores (4P+4E) · 16 GB unified · macOS 27.0.1 (26A434)
Disk: 34 GiB free of 460 GiB   <-- tight; see the open question in DECISIONS.md
Python: 3.14.6 (Homebrew) and 3.9.6 (Xcode). Homebrew 7.0.8.
Node 26.5.0 · cargo/rustc 1.96.1 · Docker 29.8.1
Absent: cmake, ffmpeg, wasm-pack, git-lfs, uv, torch
```

`torch` 2.14.1 has `cp314-macosx_14_0_arm64` wheels (verified), so the
Homebrew 3.14.6 is usable. The serving path does not need torch — it is
GGUF via llama.cpp with Metal — so torch on this machine is for scripting
only.

Project venv, as created at Stage 0:

```bash
/opt/homebrew/bin/python3.14 -m venv .venv
.venv/bin/pip install 'pydantic>=2.9' 'pyyaml>=6'
```

Installed and verified: pydantic 2.14.1, pyyaml 6.0.3.

---

## 3. The Saule toolchain

The compiler is the project's reward signal, so which binary issued a verdict
is part of that verdict.

```bash
cd ../saule
cargo build --release -p saule-cli
./target/release/saule --version      # saule 26.6-dev+9ce2dbb
```

**Do not use the `saule` on `PATH` for the harness.** At Stage 0 the installed
binary was `26.1-dev+8c0eddf` and disagreed with HEAD: `examples/markdown` and
`examples/md-viewer` exited non-zero under it and clean under HEAD. The
harness pins `data.saule.binary` and `data.saule.git_sha`, and both are
recorded in every data manifest.

Measured throughput, `saule check` on the 97 single-file fixtures (M1,
release build, process spawn per file):

| Mode | Wall | Per file | Rate |
|---|---|---|---|
| serial | 0.72 s | 7.5 ms | ~8,000 samples/min |
| `xargs -P 8` | 0.39 s | 4.0 ms | ~15,000 samples/min |

So compile-and-check scoring over 100k candidates is roughly 7 minutes, not a
bottleneck. See `docs/DECISIONS.md` ADR-0005.

### 3.1 Execution: the native VM, under OS-level isolation

The harness uses the native VM and its full capabilities — real project
directories, `import`, numsol and zenith, native packages, the whole stdlib.
`saule-wasm` is **not** part of this project (ADR-0010), so nothing here needs
`wasm-pack` or a rebuilt `.wasm`.

`saule check` is the default gate and is purely static — it never executes, so
it needs no isolation:

```bash
../saule/target/release/saule check <target> --json
```

Anything that actually **runs** a generated program needs an OS-level sandbox
first, because the native stdlib reaches `Os.execute(cmd)` and
`Os.capture(cmd)` — arbitrary shell — along with `Os.remove`, `Os.rename`,
`Os.setenv` and `Io.open` in write modes. Running model-written Saule bare on
the host is arbitrary code execution with your privileges.

This boundary is owed before the first generated program is run rather than
checked, and it is not yet built. The intended shape per tier:

| Host | Boundary |
|---|---|
| Linux (Tier 0, where synthesis happens) | a container with no network, a read-only bind of the corpus, a tmpfs scratch, and a wall-clock timeout |
| macOS (Tier 1a, for local iteration) | `sandbox-exec` with a deny-default profile, or the same container under Docker |

Unrelated note, for the repository's own sake rather than this project's: the
checked-in `www/src/lib/saule_wasm/saule_wasm_bg.wasm` dates from 2026-09-30
and predates HEAD, so the web playground runs an older compiler than the tree.
That is the playground's concern.

---

## 4. Reproducing the Stage 0 numbers

```bash
# corpus census (excludes .claude worktrees, target/, node_modules/)
.venv/bin/python scripts/corpus_census.py \
  --roots ../saule ../saule-numsol ../saule-zenith

# config schema validation
.venv/bin/python -c "import sys; sys.path.insert(0,'src'); \
from config.schema import Config; print(Config.load('configs/stage0.yaml').profile_name)"
```

---

## 5. Not yet pinned

These are deliberately absent until the stage that chooses them, so this file
does not carry versions nothing has tested:

- llama.cpp / GGUF conversion revision (Stage 6)
- whisper.cpp + Core ML encoder (Stage 8)
- the base model for the transfer track (Stage 1 gate)
- FAISS / bge-small (Stage 7)
- the aarch64 cross-compilation toolchain (Stage 13)
