# Architecture decision records

One numbered record per decision, with the options considered, the trade-off,
and the choice. A decision taken without a measurement says so.

---

## ADR-0001 — Stage 0 could not probe the training machine

**Status:** accepted, with a blocking consequence
**Date:** 2026-10-12

### Context

The mission specifies that Stage 0 proves the RX 6700 XT actually computes
before anything else happens. The Stage 0 session ran on `darwin`
(MacBookAir10,1, Apple M1, macOS 27.0.1) with the working directories
`../saule` and `saule-gpt` mounted locally. The Linux training box was not
reachable from it — not by shell, not by network path.

### Options

1. Estimate the Tier 0 numbers from the card's published specifications and
   present them as the Stage 0 result.
2. Install ROCm-adjacent tooling on the Mac and report whatever it measures.
3. Report the Mac honestly as Tier 1a, write an executable probe for Tier 0,
   and mark the GPU proof as outstanding.

### Decision

Option 3. Option 1 is the failure mode the mission names explicitly —
fabricated metrics — and option 2 measures the wrong machine. The
`HSA_OVERRIDE_GFX_VERSION` override, the fp16/bf16 question, the SDPA backend
sweep and the silent-CPU-fallback check are all properties of a ROCm
installation, and none of them exist on macOS.

### Consequence

`scripts/probe_tier0.py` exists and must be run on the Linux side before any
training is approved. It exits non-zero unless a device matmul both runs and
matches the CPU to under 1e-3. **No Tier 0 throughput figure in this
repository is measured; every one is arithmetic, labelled as such.**

What *was* measured is Tier 1a, which is a declared target in its own right,
and the entire Saule toolchain and corpus, which is where the project's real
constraint turned out to live (ADR-0005, ADR-0006).

---

## ADR-0002 — 16 GB on the M1 Air, so ~3B at Q4 is the ceiling

**Status:** accepted
**Date:** 2026-10-12

### Context

The mission asks which the Air has, because it decides the largest model that
can ship. It did not need asking: `hw.memsize` reports 17179869184 bytes.

### Decision

16 GB confirmed. Tier 1a targets a ~3B base at `Q4_K_M` with lazy-loaded
encoders, inside a 9.6 GB resident budget (60% of unified memory, as the
mission requires).

### Consequence

Recorded in `configs/stage0.yaml` as the `m1_air_16gb` profile. The 8 GB
contingency the mission describes does not apply and no 1.5B fallback is
needed for this device — though Tier 1b still gets one, since a GTX 960-class
machine is a different budget entirely.

A second measurement on this machine is less comfortable: **34 GiB free of
460 GiB.** Multimodal corpora and a Wikipedia index do not fit in that. See
the open questions.

---

## ADR-0003 — The model is a swappable backend, enforced by the config schema

**Status:** accepted
**Date:** 2026-10-12

### Context

The mission requires two interchangeable implementations behind one interface
from Stage 0, because retrofitting it later is a rewrite.

### Decision

`BackendConfig` in `src/config/schema.py` is a pydantic discriminated union on
`kind`: `scratch` (the transformer trained here, carrying `ModelConfig` and a
precision field) and `gguf` (a quantized open-weights base through
llama.cpp, carrying a quantization level and an optional LoRA path). Every
device profile names one. Nothing downstream may branch on anything but this
discriminator.

The schema also refuses incoherent combinations rather than documenting them:
a `train` section on a device declared `trainable: false` is rejected, as is a
`lora` section against a `scratch` backend.

### Verification

Nine negative paths were run against the validator and all nine were rejected,
including a one-character key typo, an undeclared `active_device`, a `d_model`
not divisible by `n_heads`, and a `train` section on the M1 profile.

---

## ADR-0004 — `modality_mode` defaults to `text`

**Status:** accepted
**Date:** 2026-10-12

### Decision

`ModalityConfig.mode` is `text` by default, with `embed` and `hybrid`
available. Audio routes through Whisper ASR and images through a captioner,
injected as plain text.

### Rationale

At this scale a small model frequently ignores injected soft tokens entirely,
and the mission requires the learned path to beat the text baseline before it
ships. Defaulting to `text` makes the fallback the starting point rather than
a retreat, so Stages 8 and 9 have something to measure against from day one.

Encoders are `lazy_load: true` on every serving profile: holding Whisper and
CLIP resident alongside a 3B model in 9.6 GB is not affordable.

---

## ADR-0005 — Compile-and-check scoring is not the bottleneck; generation is

**Status:** accepted
**Date:** 2026-10-12

### Context

The mission expects synthesis scoring over 100k candidates to be CPU-bound on
the Ryzen's 6 cores, and asks for a throughput figure. It also proposes
`saule-wasm` as the sandbox specifically to avoid per-sample container
overhead.

### Measurement

`saule check` over the 97 single-file fixtures, release build from HEAD, on
the M1 — one process spawn per file, the *slowest* plausible harness design:

| Mode | Wall | Per file | Rate |
|---|---|---|---|
| serial | 0.72 s | 7.5 ms | ~8,000 samples/min |
| `xargs -P 8` | 0.39 s | 4.0 ms | ~15,000 samples/min |

### Decision

100k candidates score in roughly 7 minutes. Scoring is not a bottleneck and
does not need `saule-wasm` for *speed*.

(Superseded in part by **ADR-0010**: this ADR originally went on to say that
`saule-wasm` was still wanted for *isolation*. It is not used at all now — the
harness uses the native VM, and isolation is an OS-level boundary the harness
owns. The throughput measurement above is unaffected.)

The real cost of synthesis is the generator writing candidate Saule, not the
compiler judging it. That reframes the Stage 1 decision: it is a question
about generation quality and wall clock, not about scoring throughput.

### Note

Measured on an M1, not on the Ryzen 5 7600. The figure is expected to be the
same order on either; it is not a Tier 0 measurement.

---

## ADR-0006 — Bytecode dedup needs a text fallback, because `disasm` is single-file

**Status:** accepted
**Date:** 2026-10-12

### Context

The mission requires dedup on bytecode rather than text, on the grounds that
two textually different programs compiling to the same bytecode are one
sample. That assumes every corpus file can be disassembled.

### Measurement

`saule disasm` at HEAD (`9ce2dbb`), per corpus group:

| Group | Files | Disassemble | Note |
|---|---|---|---|
| `tests/*.sau` | 97 | **97** | single-file programs |
| `benchmarks/` | 16 | **16** | single-file programs |
| `examples/` | 68 | **14** | multi-file projects |
| numsol + zenith `src/` | 12 | **2** | multi-file libraries |

The failures are not broken code — the same projects type-check clean in
project mode. They are resolution failures from compiling a file in isolation:
`cannot compile a class extending one the compiler cannot see` (17),
`cannot compile a name the resolver could not classify` (11),
`cannot compile an import in a program with no file behind it` (10).

### Decision

`DataConfig.dedup_on` defaults to `bytecode_then_text`: bytecode where the
compiler produces it, canonical formatted text where it does not. The
fallback is not optional, and the manifest records which key was used per
sample.

### Consequence — lifted 2026-10-12

**This compromise no longer applies.** `saule disasm` now takes a project and
routes through the program driver, so a module belonging to an import graph is
compiled with that graph. Every example project and both sibling libraries
disassemble: 49 chunks across the project runs, against 14 of 68 example files
and 2 of 12 library files when this ADR was written. See
`docs/SAULE_TOOLCHAIN_GAPS.md` gap 1 for what landed.

`DataConfig.dedup_on` stays `bytecode_then_text`, because a project importing
an uninstalled native package still cannot be compiled and the fallback has to
exist. But the text path is now the rare case rather than the one covering
19,971 lines, and bytecode is the key for essentially all real samples.

One rule the fix introduces, in `CLAUDE.md` §5.3: hash the chunk **body**, not
its `; chunk from <label>` header. Inside a project the label is
project-relative and reproducible; outside one it is an absolute path. Two
identical programs in differently-named files must dedup to one sample.

### Consequence as originally written

The 19,971 lines in `examples/` — the most idiomatic, project-shaped Saule in
the corpus — and both sibling libraries get the weaker dedup key. Any
synthetic generator that emits multi-file projects inherits that weakness.
Lifting it requires a project-mode disassembly from the toolchain; see
`docs/SAULE_TOOLCHAIN_GAPS.md`, gap 1.

`saule fmt` was verified idempotent on all 97 fixtures (`fmt(fmt(x)) ==
fmt(x)`, 97/97), so it is a sound canonicaliser. Worth knowing: only 4 of the
97 fixtures are already in canonical form, so the training corpus will differ
textually from the repository as committed.

---

## ADR-0007 — The eval fixtures are split before any generator runs

**Status:** accepted
**Date:** 2026-10-12

### Measurement

`scripts/corpus_census.py`, excluding `.claude/worktrees`, `target/` and
`node_modules/`:

| Group | Files | Lines | KiB | Role |
|---|---|---|---|---|
| `examples/` | 71 | 19,971 | 526.9 | idiomatic multi-file projects |
| `tests/*.sau` | 97 | 3,692 | 85.3 | **eval set**, 1:1 with `expected/*.out` |
| library `src/` | 12 | 3,059 | 83.5 | numsol + zenith |
| `tests/ui/` | 157 | 1,914 | 46.0 | broken-on-purpose, repair seeds |
| `benchmarks/` | 16 | 696 | 14.9 | single-file programs |
| **total** | **353** | **29,332** | **756.5** | |

The eval fixtures are 11.3% of the corpus by bytes.

### Decision

The 97 fixtures and their 97 `expected/*.out` files are split out as the first
action of Stage 1, before any generator is written, and are excluded from
every generator's seed pool by construction rather than filtered afterwards.
`GeneratorConfig.seed_groups` names which groups a generator may draw from and
cannot name the eval group.

### A trap found while measuring

A naive `find . -name '*.sau'` over the Saule repository returns 841 files.
**503 of them live under `.claude/worktrees/`** — agent git worktrees holding
complete copies of the repository, test fixtures included. Globbing them in
trains on every fixture twice, defeats dedup, and contaminates the eval split
with byte-identical copies. `scripts/corpus_census.py` excludes them by name,
and any dataset builder must do the same.

---

## ADR-0008 — The natural corpus is ~215k tokens, which settles the track order

**Status:** accepted
**Date:** 2026-10-12

### Measurement

Trainable Saule with the eval fixtures held out: 671.2 KiB, about **215k
tokens** at a crude 3.2 bytes/token. Stage 2 replaces that estimate with a
real tokenizer measurement. Prose adds ~49.9k words (41 doc pages at 30,903
words, plus README and DOCS at 18,961) — call it another ~67k tokens.

### What that implies

At the Chinchilla floor of 20 tokens per parameter, 215k tokens is the
optimal budget for a model of roughly **11,000 parameters**. The from-scratch
target is 129M. The gap is four orders of magnitude, and no filtering or
augmentation of the natural corpus closes it.

### Decision

The track order the mission settled — transfer primary, from-scratch as a
measured control — is confirmed by measurement rather than accepted on
assertion. From-scratch pretraining on Saule alone is not a capability
proposal; its value is a floor for the eval harness and an owned
implementation.

It also confirms the mission's own framing: synthesis is the project. Every
token beyond the first ~280k has to be generated and verified.

---

## ADR-0009 — zenith cannot train anything here, and new layers would not change that

**Status:** accepted
**Date:** 2026-10-12

### Context

A question was raised about gaps in the numsol (numpy-shaped) and zenith
(torch-shaped) Saule libraries, with an offer to extend them.

### Measurement

Both libraries type-check clean at HEAD (numsol 7 files, zenith 5 files) and
both self-asserting example suites pass: zenith's `gradcheck` reports 55/55
agreeing with finite differences, and `xor` trains two networks (0.57 s wall).
The autograd engine is correct.

Throughput, numsol `matmul` at 128×128, 20 iterations, startup subtracted:

```
wall=1.976s  minus startup=1.869s  flop=83.9 MFLOP
numsol matmul: 44.9 MFLOP/s
```

For scale, that is roughly 1,100–2,200× slower than this M1's own CPU in
optimised fp32, and ~580,000× slower than the RX 6700 XT's fp16 peak.

### Decision

zenith is not a candidate for training any model in this project, and the
reason is not a missing API. A 129M-parameter step is about 744 MFLOP per
token forward+backward; at 44.9 MFLOP/s that is ~17 seconds per token, so a
2.0B-token run is on the order of a thousand years. Adding attention,
convolution or batched `matmul` to zenith would not move that number, because
the limit is `kernel.sau` — pure Saule loops on a register VM — not the layer
catalogue.

Both READMEs already say exactly this, unprompted: "`kernel.sau` is the single
file a native backend replaces."

### Consequence

The libraries' role in this project is as **corpus and doc-grounded task
material** — 3,059 lines of idiomatic, fully type-annotated, clean-checking
Saule, plus two READMEs — and potentially as sensor math in the Tier 2 robot
API. Neither role needs new layers. If zenith is to matter computationally,
the work is a native `kernel.sau` backend, which Saule's native-package system
already supports; see `docs/SAULE_TOOLCHAIN_GAPS.md`, gap 3. That is a real
project and it is not on this project's critical path.

---

## ADR-0013 — Batched `matmul` and `any`-operand operators in accel

**Status:** accepted — built 2026-10-13
**Date:** 2026-10-13

### Context

ADR-0012's op-set list opened with two items: rank-3 batched `matmul`, which
multi-head attention needs, and operators that take `any`, without which
`accel` could not stand in for `numsol` because zenith writes `a * 2.0` and
`values - values.maxAxis(2, true)` throughout.

### `any` operands

`SValue` is the SDK's spelling of `any`, and `SValue::Object(SAnyObject)`
narrows to a native class with `downcast::<Arr>()`. So one method takes all
three right-hand sides numsol accepts:

| right-hand side | handled as |
|---|---|
| another `Arr` | borrowed, broadcast elementwise — no copy |
| a lone `float` | applied directly, without building an array for it |
| a `table<float>` | copied once, read as rank 1 |

`implements` is now `OpAdd<any, Arr>` and the rest, matching numsol's
`OpAdd<any, NDArray>`. `a + 2`, with an **integer**, is refused by name —
*"elements are `float`, so `2` has to be written `2.0` or `2f`"* — because
Saule never promotes `integer` to `float` and this layer does not get a
private exemption. It surfaces as a type error rather than a thrown string, so
it is not catchable and is covered by the Rust tests.

### Batched `matmul`

| left | right | result | used for |
|---|---|---|---|
| `(M, K)` | `(K, N)` | `(M, N)` | an ordinary projection |
| `(B, M, K)` | `(B, K, N)` | `(B, M, N)` | per-head attention |
| `(B, M, K)` | `(K, N)` | `(B, M, N)` | one weight across a batch |

The third form matters on its own: a transformer's projections apply the same
matrix to every sequence, and without it the caller must broadcast the weight
into `B` copies first — `B` times the memory and `B` times the reads.

Each batch delegates to the existing tiled rank-2 kernel, so the tiling is the
one already tested rather than a second implementation.

A rank-1 right operand follows numpy's rule: promoted to a **column**,
multiplied, then the added axis dropped, so `(M,K) @ (K,) -> (M,)`. The first
version promoted it to a *row*, which type-checks and is silently wrong; the
test that compares against the per-slice product caught it.

### `transposeLast`

`transpose` reverses every axis — correct for rank 2, and on a batch it turns
`(B, L, d)` into `(d, L, B)`, moving the batch into the middle. Attention needs
`(B, L, d) -> (B, d, L)`, so `transposeLast` swaps only the last two. numpy
spells it `.mT`. This was not on ADR-0012's list and only appeared once batched
`matmul` existed to need it.

### Verified

- 34 Rust tests, clippy `-D warnings` clean, fmt clean.
- Batched products checked against the rank-2 product of each slice — the
  definition, and how a caller would check by hand.
- `examples/operators.sau`: **17/17** from Saule, ending with the real shapes —
  `Q @ Kᵀ` on `(2,4,3)` giving `(2,4,4)`, then `@ V` giving `(2,4,3)`.
  **Multi-head attention is now expressible.**
- `examples/conformance.sau`: still **32/32** against numsol after the API
  change, so widening to `any` broke nothing.

### What is still owed

Of ADR-0012's list, items 1 (rank-3 `matmul`) and the `any` prerequisite are
done. Remaining: an embedding op with a scatter-add backward, gradient
clipping, checkpoint save/load, and an LR schedule. zenith is still untouched
and still runs on pure-Saule numsol — wiring it to `accel` is the next step,
and `toygpt`'s two loss curves are the test that it changed nothing.

---

## ADR-0012 — A GPT trains in Saule, and here is exactly what the op set lacks

**Status:** accepted — built 2026-10-13
**Date:** 2026-10-13

### Context

Before any backend work, one question had to be answered: does the stack train
at all? A correctness baseline had to exist before there was a backend to
blame when something went wrong, and the missing-operation list had to come
from the compiler rather than from guesswork.

### What was built

`saule/toygpt`, a decoder-only transformer in Saule on zenith's autograd:
learned token and positional embeddings, pre-norm blocks of causal
self-attention and a GELU MLP, a final norm, an output projection, trained
with cross-entropy on next-token prediction. 2 layers, d=16, vocab 4, seq 6 —
20 tensors, 4,400 scalars.

It asserts its own outcome rather than printing a curve, and runs two tasks:

| Task | Result |
|---|---|
| cycle `1→2→3→4→1` | loss **1.665 → 0.00010**, 4/4 continuations correct |
| lookback: copy token 1 to the end across identical filler | loss **1.857 → 0.000017**, 3/3 correct |

The second one is the load-bearing test. Every position except the first
carries the same filler token, so the final prediction cannot be made from the
previous token, the position, or any short window — the only way to answer is
to attend back to position 1. A model whose attention did nothing would pass
the cycle task and score 0/3 here. **Attention works.**

Initial loss was 1.665 and 1.857 against `ln(4) = 1.386`. In the right place
for a uniform guess, which is the init-loss check the brief asks for; the
excess is the untrained positional and norm parameters.

Whole run: **56 s** on pure-Saule numsol at 44.9 MFLOP/s.

### What had to be built by hand, because zenith lacks it

- **RMSNorm** — from `pow`, `meanAxis(2, true)`, `sqrt`, `div`, `mul`.
  Preferred to LayerNorm: no mean subtraction, no bias.
- **GELU** — the tanh approximation, since there is no `erf`.
- **Causal mask** — a constant `NDArray` of 0 and a large negative, *added*
  before softmax rather than multiplied after, so the surviving weights still
  sum to one.
- **Embedding lookup** — `Metrics.oneHot(tokens, vocab) @ wte`, because there
  is no gather. Correct, and wasteful by a factor of the vocabulary.

### The compromises the current array layer forces

- **One attention head.** `matmul` is rank 2, so `(L,d) @ (d,L) -> (L,L)` for
  a single sequence is expressible and a batched multi-head product is not.
- **One sequence per forward pass**, for the same reason. Harmless here
  because gradients accumulate on the leaves, so looping before `step()` is
  ordinary gradient accumulation.
- **No head splitting**, because `Tensor` has no `slice`.

### The op set still owed, in dependency order

1. **Rank-3 batched `matmul`** — unlocks multi-head and real batching. The
   single biggest item, and it belongs in numsol/accel, not zenith.
2. **An embedding op with a scatter-add backward** — removes the one-hot
   waste, and needs integer arrays or at least integer indices.
3. **Gradient clipping** — absent, and required before a run long enough to
   meet a loss spike.
4. **Checkpoint save/load** — absent. `Linear.setWeight` and
   `Tensor.setValue` are the hooks. Required before any multi-day run, which
   makes it a prerequisite for the from-scratch model rather than a nicety.
5. **An LR schedule** (cosine with warmup) and optionally dropout and weight
   tying.

### Consequence

The architecture is validated, so every later phase is measured against an
unchanged loss curve on these two tasks — which is the point of keeping the
device switch behind the API. Nothing about the model code changes when accel
or a GPU lands beneath it.

Two gotchas cost time and are now in `CLAUDE.md` §6: `oneHot` is on
`Metrics`, not `Loss`; and `#inputs as float` parses as
`#(inputs as float)` — it needs `(#inputs) as float`.

---

## ADR-0011 — A native kernel layer for numsol: measured, and what it does not buy

**Status:** accepted — built 2026-10-13
**Date:** 2026-10-13

### Context

The user asked for a device switch — `autoUseGPU(true/false)` — with all
backend logic behind the API so model code never names a device, and for
numsol and zenith to be "blazing fast": numsol as quick as numpy, zenith as
quick as torch. Everything written from scratch, with no framework doing the
work.

### What was built

`~/Documents/rust/saule-accel`, a native package on `saule-sdk`, installed as
one `.dylib` in `~/.saule/native_packages/`:

- `Arr`, a dense row-major `f64` array whose **buffer lives in Rust**. The SDK
  copies a `Vec<T>` across the boundary, so a design where Saule held the data
  and handed it to a native kernel per operation would marshal the whole
  buffer both ways on every call — 8 MB each way on a million elements, more
  than the arithmetic it was meant to accelerate. An `Arr` crosses as an
  opaque handle instead; `a.matmul(b)` passes two handles and the floats never
  move. `toTable` is the only method that copies, for the edges.
- Hand-written kernels in `src/kernel.rs`: tiled GEMM, broadcasting
  elementwise with fast paths for equal shapes and for a scalar, blocked
  transpose, axis reductions. No BLAS, no framework.
- `Accel.autoUseGPU(bool)` returning the device **actually** selected, so it
  cannot promise what the build lacks. It returns `"cpu"` today and
  `Accel.gpuAvailable()` is `false`, because the GPU backend is not written
  yet. The switch is wired and truthful rather than aspirational.

### Conformance

numsol is the oracle. 32 checks across construction, shapes, broadcasting,
elementwise, reductions and `matmul`, each computing the same thing twice and
comparing element for element: **32/32 pass, worst difference 0.0** on all but
`tanh`, which differs by 1.1e-16 — one ULP, expected of a transcendental.
Checksums over the benchmark agree exactly.

### Speed, measured on the M1

Against pure-Saule numsol, 128², release interpreter both sides:

| | MFLOP/s |
|---|---|
| numsol | 44.86 |
| accel | 8,028 |
| speedup | **179x** |

A first measurement of 4,307x was wrong: it compared against a **debug** build
of the interpreter. The release figure above is the honest one, and it
reproduces the 44.9 MFLOP/s recorded at Stage 0 exactly.

### Against numpy, which is the stated bar

numpy 2.5.4 on Apple Accelerate, same machine, f64:

| Size | accel | numpy | gap |
|---|---|---|---|
| 128² | 12,142 | 34,408 | 2.8x |
| 512² | 6,951 | 113,588 | 16.3x |
| 1024² | 5,092 | 134,108 | 26.3x |

**"As fast as numpy" is not met, and the gap widens with size** — accel
degrades from 12.1 to 5.1 GFLOP/s while numpy improves from 34 to 134. That
shape is diagnostic rather than mysterious: single-level tiling with no panel
packing, no SIMD intrinsics, bounds-checked inner loops, one thread. numpy's
f32 reaches 920 GFLOP/s at 512² via Apple's AMX matrix coprocessor, which
portable code cannot reach at all.

The remaining headroom, in the order it pays: multi-level blocking with panel
packing (the standard GEMM structure, and the fix for the widening gap),
an explicit NEON/AVX microkernel, multithreading, then `f32`. Parity with
numpy's **f64** is conceivably reachable; parity with its **f32** on this
machine is not, because that number is Accelerate using hardware a portable
kernel cannot address.

### What this does not buy, stated plainly

It does not make the from-scratch GPT trainable on the CPU. A 129M model needs
~2.0e18 FLOP; at 8 GFLOP/s that is **7.9 years**, and even at an optimistic
100 GFLOP/s with threads and SIMD it is ~231 days. **A GPU backend is
required**, and it is the next piece of work, not an optimisation.

This confirms rather than overturns **ADR-0009**, which said zenith's limit was
`kernel.sau` and not its layer catalogue. Replacing `kernel.sau` bought 179x
exactly as that ADR implied it would. What has changed is that the ceiling is
now worth chasing: with a GPU backend in the 2–5 TFLOP/s range, training the
from-scratch GPT in the user's own stack moves from arithmetically impossible
to the same order as PyTorch on the RX 6700 XT.

### Consequence

`numsol` stays pure Saule and dependency-free, and remains the correctness
oracle rather than being deleted — that comparison is the entire reason a
disagreement would be caught. The native package is opt-in by import. zenith is
untouched so far: its autograd stays in Saule, which costs nothing, because one
`(8192,768)@(768,3072)` matmul is ~19 ms at 2 TFLOP/s while a few thousand
boundary crossings are milliseconds.

---

## ADR-0010 — The harness uses the native VM, not `saule-wasm`

**Status:** accepted — the user's decision, overriding the mission brief
**Date:** 2026-10-12

### Context

The mission brief named `saule-wasm` as the scoring sandbox: "the whole
language as a WASM module, source in, JSON out. This is your sandbox.
Compile-and-run scoring over 100k candidates is a module call, not a
container."

Stage 0 found that route restricted. `saule_wasm::run` seeds no modules — its
own comment reads "resolving imports needs a filesystem, and there isn't one.
A program that imports gets a compile error" — and uses the single-module
compile route rather than `program::compile`. So it cannot score a multi-file
sample, nor anything importing numsol or zenith.

### Decision

Drop `saule-wasm` from the harness entirely and use the native VM's full
capabilities: real project directories, `import`, both sibling libraries,
native packages, the whole stdlib.

This was the user's call, not an inference from the measurement. It is
recorded here because it overrides the brief, and because the brief's own rule
is that a scope change is raised rather than made quietly.

### What it buys

Multi-file synthetic projects become scoreable, and so do doc-grounded tasks
over numsol and zenith — 3,059 lines of the best-annotated Saule in the
corpus, reachable only through `import`. The WASM route could not score either
at all. It also raises the value of closing gap 1 (project-mode `disasm`),
since multi-file samples are now first-class rather than an edge case.

It also removes a dependency Stage 0 found unhealthy anyway: `wasm-pack` is
not installed, and the checked-in `saule_wasm_bg.wasm` predates HEAD, so the
playground runs an older compiler than the repository. That is now purely the
playground's concern, not the harness's.

### What it costs, and the rules that follow

The native stdlib reaches `Os.execute(cmd)` and `Os.capture(cmd)` — arbitrary
shell execution — along with `Os.remove`, `Os.rename`, `Os.setenv`,
`Os.chdir`, `Os.exit` and `Io.open` in write modes. Executing model-written
Saule on the host is therefore arbitrary code execution with the user's
privileges. The WASM module had none of that surface: `native-packages` off,
no `dlopen`, output captured.

Two binding rules, recorded in `CLAUDE.md` §5.4:

1. **`saule check` is the default gate.** It is purely static and never
   executes, so compile-rate scoring, the error-repair triples and
   type-correctness carry no exposure at all. Measured at ~15,000 samples/min,
   this is the bulk of the pipeline.
2. **Execution happens only under OS-level isolation** — container, `seccomp`
   or `sandbox-exec` — never bare on the host. This is harness-side work owed
   before the first generated program is *run* rather than checked, and it is
   now the only isolation in the design.

### Consequence

Gap 3 in `docs/SAULE_TOOLCHAIN_GAPS.md` is **dropped, not deferred**: there is
no reason to teach `saule-wasm` in-memory modules for this project's sake. If
it is ever done, the justification will be the playground — letting the docs'
numsol and zenith examples run in the browser — which is a product reason, not
a training one.

---

## Open questions for the Stage 0 gate

Each carries a recommended default, so "defaults" is a complete answer.

1. **Run the Tier 0 probe.** Nothing about training can be approved without
   it. *Default: you run `scripts/probe_tier0.py` on the Linux side and paste
   the output; I proceed to Stage 1 planning meanwhile, since Stage 1 is
   CPU-and-network work that does not depend on the GPU.*

2. **Container or native ROCm?** *Default: the `rocm/pytorch` container, flags
   in RUNBOOK §1.3 — the host keeps only the kernel driver and a bad ROCm
   release is discardable.*

3. **Disk.** The Mac has 34 GiB free of 460 GiB, and the Linux box is
   unmeasured. A Wikipedia index plus multimodal corpora does not fit in 34
   GiB. *Default: tell me the Linux box's free space; keep all bulk data
   there, and on the Mac keep only the quantized model, the doc index and the
   Saule corpus — which is 756 KiB, so it is free.*

4. **Target model size.** 12L/768d with `d_ff=2048` is 100.7M; `d_ff=3072` is
   129.0M, which is the ~124M the mission names. *Default: `d_ff=3072` for
   `target`, 6L/384d/`d_ff=1024` (22.0M) for `dev`.*

5. **Which base model for the transfer track?** Not chosen at Stage 0 because
   it belongs with the Stage 1 data decision. *Default: Qwen2.5-Coder-3B for
   the Air, Qwen2.5-Coder-1.5B for the GTX laptop, both LoRA fp16 in 12 GB.*

6. **Generation strategy for synthesis** — local Q4 7–14B, a frontier API for
   generation only, or a mixture. This is the project's real cost centre now
   that scoring is measured cheap. *Default: I bring you three worked options
   at the Stage 1 gate with a quality sample and a wall-clock estimate each,
   rather than picking one now.*

7. **Tier 1b and Tier 2 hardware.** The GTX laptop is unprobed, and no board
   is chosen. *Default: probe the GTX laptop at Stage 6 when export paths are
   benchmarked; defer the board choice until Stage 12, and buy nothing until
   the assistant earns it.*

8. **Audio and vision.** They sit furthest from a Saule coding assistant.
   *Default: defer Stages 8–10 until the transfer assistant passes its eval
   gates; keep `modality_mode: text` wired so nothing blocks later.*
