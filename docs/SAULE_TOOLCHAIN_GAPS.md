# Saule toolchain gaps that block the data pipeline

Measured at Stage 0 against `saule 26.6-dev+9ce2dbb`, built from source. Each
gap says what breaks without it, how big the loss is in corpus terms, and what
the smallest fix looks like. Nothing here is a complaint about the language —
the toolchain is a better starting position than most projects get, and three
of these four are consequences of features that simply stop at a boundary the
training harness happens to need crossed.

Ordered by how much they cost this project.

---

## Gap 1 — `saule disasm` is single-file — **CLOSED 2026-10-12**

**Fixed.** `disasm` now routes through the program driver, so a module that
belongs to an import graph is compiled together with that graph rather than on
its own.

It needed **no change to `saule-vm` at all** — the machinery was already
there and only the CLI was using the wrong entry point:

- `saule_vm::program::compile(entry) -> Program { modules, entry, via }`
  already compiled a whole import graph, post-order, with every chunk sharing
  one `Rc` for classes/enums/interfaces so a `ClassIdx` means the same thing
  across all of them;
- `saule_vm::disasm::chunk(&Chunk) -> String` was already public, and
  `disassemble()` was only ever `compile()` followed by it.

What landed in `../saule`:

- `saule disasm [TARGET] [--deps]`, with `run`'s and `check`'s target rule — a
  directory or no argument means project mode, a file means that file.
- Project mode roots at **every** source file in turn rather than only the
  entry, matching why `check` walks every file, and deduping modules so each
  is emitted once. A library therefore needs no entry point.
- `--deps` adds the imported modules for a single-file target.
- One unusable file in project mode no longer stops the rest: compiled chunks
  print, failures go to stderr, exit status 1.
- Documented in `www/src/content/docs/reference/cli.md`, which had no `disasm`
  section before.

Measured after the change, across all 14 example projects and both sibling
libraries: **49 chunks** emitted, every project covered. Before: 14 of 68
example files and 2 of 12 library files. The four projects still failing all
import `shine`, a native package that is not installed — reported clearly, and
not a compiler limitation.

Verified: 1795 workspace tests pass (10 new integration tests through the real
binary, plus 2 arg-parsing), clippy clean under
`--workspace --all-targets -D warnings`, `cargo fmt --check` clean, 254/254
fixtures.

**One consequence for the harness**, recorded in `CLAUDE.md` §5.3: hash the
chunk *body*, not the `; chunk from <label>` header. Inside a project the
label is project-relative and reproducible; outside one it is an absolute path,
which is machine-specific. Two identical programs in differently-named files
must dedup to one sample either way.

<details>
<summary>The original finding, kept for the record</summary>

**Priority: high. This one has no workaround that preserves the guarantee.**

### What was measured

| Group | Files | Disassemble | Share |
|---|---|---|---|
| `tests/*.sau` | 97 | 97 | 100% |
| `benchmarks/` | 16 | 16 | 100% |
| `examples/` | 68 | 14 | 21% |
| numsol + zenith `src/` | 12 | 2 | 17% |

The 54 failing example files and 10 failing library files are **not broken**.
The same projects type-check clean under `saule check <dir>`. They fail because
`saule disasm <file>` compiles one file with no import graph behind it:

```
17 ×  cannot compile a class extending one the compiler cannot see
11 ×  cannot compile a name the resolver could not classify
10 ×  cannot compile an import in a program with no file behind it
 5 ×  cannot compile a named argument to a callee the compiler cannot identify
```

### Why it matters

Bytecode is the dedup key that makes "two textually different programs that
compile to the same bytecode are one sample" true, and it is what makes a
refactor *verified* rather than plausible. Without it the fallback is canonical
formatted text, which cannot see through a renamed local or a reordered
declaration.

The files that lose the guarantee are the 19,971 lines in `examples/` — the
most idiomatic, project-shaped Saule in existence — plus both sibling
libraries. They are also exactly the shape a synthetic generator must produce,
since a code assistant for a project-structured language has to emit
multi-file projects.

### Smallest fix

A project-mode disassembly that resolves imports the way `check` already does:

```
saule disasm <dir>              # every chunk in the project, in a stable order
saule disasm <file> --project   # that file, with the project's import graph seeded
```

`saule check <dir>` already builds the resolution context this needs, and
`saule-vm` already has a `program::compile` path for import graphs that the
single-module route in `saule-wasm` deliberately bypasses. The ask is to reach
that path from the CLI, not to write a new compiler mode.

A stable textual form is enough; the harness hashes it. It does not need to be
a user-facing format, and the existing "report gaps by name rather than
silently omit" behaviour is the right contract to keep.

</details>

---

## Gap 2 — no machine-readable diagnostics from the CLI — **CLOSED 2026-10-12**

**Fixed.** `saule check --json` now emits the structured form, and the shape
lives in a new leaf crate, `saule-diagnostics`, that `saule-wasm` was
refactored onto so the playground and the CLI cannot drift apart.

What landed, in `../saule` on `develop`:

- `crates/saule-diagnostics` — `Span`, `Severity`, `Phase`, `Diagnostic` and a
  `to_diagnostic` generic over `miette::Diagnostic`, so the span, help and code
  come off the trait and a new error variant anywhere needs no change. Depends
  on `miette` and `serde` only. 9 tests.
- `saule check --json` — a per-file envelope with compiler provenance
  (`saule_version`, `commit`), every file examined including the clean ones,
  and `summary` counts. Exit status unchanged. 12 integration tests through the
  real binary.
- Two new phases the playground never needed: `io` (a missing or unreadable
  file — not a fault in the code) and `import` (an unresolved `import` — the
  fix is a missing file or dependency).
- Documented in `www/src/content/docs/reference/cli.md`, which had no `check`
  section at all before.

Verified: 1783 workspace tests pass, clippy clean under
`--workspace --all-targets -D warnings`, `cargo fmt --check` clean, and all 254
fixtures behave as expected.

**Still open:** no error variant declares a `#[diagnostic(code(...))]`, so the
`code` key is absent on every diagnostic and `phase` remains the only
classification. Wiring 112 codes is a taxonomy decision for the language's
author, not something to do unilaterally — and one constraint found the hard
way: `code(...)` is parsed as a Rust path, so no segment may be a keyword.
`code(saule::type::bad_cast)` does not compile; spell it `types`.

<details>
<summary>The original finding, kept for the record</summary>

**Priority: high. Cheap to fix, and the error-repair generator depends on it.**

### What was measured

`saule check` has exactly one option, `--dump-type-coverage`. Diagnostics come
out as miette's rendered form:

```
  × cannot assign value of type `any` to variable of type `integer`
   ╭─[any_to_concrete_needs_cast.sau:6:22]
 5 │         local a: any = "i am a string"
 6 │         local n: integer = a
   ·                            ┬
   ·                            ╰── incompatible type
 7 │         println(n)
   ╰────
  help: the value's type isn't compatible with the declared type
```

The error types carry `#[diagnostic(help(...))]` but **no `code(...)`**, so
there is no stable identifier for an error kind.

### Why it matters

The strongest generator in the plan is error repair: mutate a working program,
run `saule check`, capture `(broken source, exact diagnostic, fix diff)`. That
needs the diagnostic as data — message, phase, and the byte span to locate the
fix — for hundreds of thousands of samples. Parsing box-drawing characters is
fragile in a way that fails quietly: a regex that silently stops matching
produces a dataset of unlabelled broken programs, and nothing downstream
notices.

Stable error codes matter separately: without them the generators cannot be
balanced across diagnostic kinds, and the eval harness cannot report
repair accuracy per error class. "Did it fix a type error or a scope error"
becomes string matching on prose that may be reworded at any time.

### Smallest fix

**The JSON contract already exists.** `crates/saule-wasm/src/lib.rs` defines
precisely the right shape and a mapping from every pipeline error type into it:

```rust
pub struct Diagnostic {
    pub severity: &'static str,
    pub phase: Phase,          // lex | parse | semantic | type | compile | runtime
    pub message: String,
    pub span: Option<Span>,    // byte offsets
    pub help: Option<String>,
}
```

Two asks, in order of value:

1. `saule check --json` (and `saule run --json`) emitting that existing
   structure, lifted out of `saule-wasm` into a shared crate so the CLI and the
   WASM build cannot drift apart.
2. A `code` field — `#[diagnostic(code(saule::types::nullable_assign))]` or
   similar — stable across rewordings. Even a coarse taxonomy helps.

</details>

---

## Gap 3 — `saule-wasm` cannot score anything that imports — **DROPPED 2026-10-12**

**Will not be done, and nothing in this project needs it.** The user's
decision: the harness uses the native VM and its full capabilities — real
project directories, `import`, numsol and zenith, native packages, the whole
stdlib. `saule-wasm` is not part of the harness at all, so its inability to
seed modules is no longer a gap in anything. See `docs/DECISIONS.md` ADR-0010.

This is a simplification, not a concession. The WASM route could not score a
multi-file sample or a doc-grounded task over either sibling library; the
native VM scores both.

Two things carry forward from the analysis below, and they matter more than
the gap did:

- **`saule check` is the default gate**, because it is purely static and never
  executes. Compile-rate scoring, error-repair triples and type-correctness
  therefore carry no execution exposure whatsoever.
- **Execution requires OS-level isolation.** The native stdlib reaches
  `Os.execute` and `Os.capture` — arbitrary shell — plus `Os.remove`,
  `Os.rename`, `Os.setenv` and `Io.open` in write modes. Running model-written
  Saule bare on the host is arbitrary code execution with the user's
  privileges. A container, `seccomp` or `sandbox-exec` boundary is owed before
  the first generated program is run rather than checked. That is harness-side
  work in `saule-gpt`, not a change to Saule.

If `saule-wasm` ever gains in-memory modules, the reason will be the
playground — making the docs' numsol and zenith examples runnable in the
browser — which is a product argument, not a training one.

<details>
<summary>The original analysis, kept for the record</summary>

**Priority: medium. There is a workaround, but it costs the sandbox.**

### What was measured

`saule_wasm::run` seeds no modules, and says so in its own comment: "resolving
imports needs a filesystem, and there isn't one. A program that imports gets a
compile error." It calls `saule_semantic::ModuleSeed::default()` and
`saule_vm::compile` on a single module.

### Why it matters

`saule-wasm` is the sandbox — `native-packages` is off, so no `dlopen`, and
output is captured rather than written to a real stdout. It is the right place
to execute a hundred thousand generated programs.

But any sample that `import`s is rejected before it runs, which excludes:

- every multi-file synthetic project;
- every doc-grounded task built on numsol or zenith, which are reachable only
  by `import * from "numsol"`. The plan explicitly calls for those, and the
  libraries are 3,059 lines of the best-annotated Saule available.

### The workaround, and its cost

A real temp project directory plus `saule check` / `saule run` works today —
verified at Stage 0 by building a throwaway project that imports numsol and
runs:

```
checked 1 file: no errors
[
 [1.0000, 3.0000, 5.0000]
 [7.0000, 9.0000, 11.0000]
]
```

At ~15,000 checks/min this is fast enough. What it loses is isolation:
`saule run` on the host executes generated code against the full stdlib,
including `Io`, `File` and `Os`. For code a language model wrote, that is a
real exposure, and it is the one place in this pipeline where "it is only test
data" is not a good answer.

### Smallest fix

Let `saule-wasm` accept a virtual module map, so the sandbox keeps its
isolation and gains imports:

```rust
pub fn run_with_modules(source: &str, modules: &[(String, String)]) -> RunResult
```

`ModuleSeed` is already the parameter `analyze_with_bindings` takes, and
`saule-vm` already has an import-graph compile path. The harness would hand it
numsol and zenith as source strings and never touch a filesystem.

(Later scoping, before the gap was dropped: the front-end half is easy, but
`program::compile(entry: &Path)` resolves through the real filesystem
throughout, so in-memory modules would need file access abstracted behind a
trait and threaded through the import walk — a change to the compiler's entry
points rather than an addition beside them.)

</details>

---

## Gap 4 — numsol/zenith: the gap is the kernel, not the API

**Priority: low for this project. Real work, off the critical path.**

This is the honest answer to "what should we add to numsol and zenith".

### What was measured

Both libraries are in good shape. Both type-check clean at HEAD. zenith's
`gradcheck` passes 55/55 against finite differences to ~1e-11, and `xor`
trains two networks and asserts its own outcome. The autograd engine — tape,
`unbroadcast`, fused `crossEntropy` and `bceWithLogits` gradients — is
correct, and the design is sound.

The limit is throughput. numsol `matmul`, 128×128, 20 iterations, process
startup subtracted:

```
wall=1.976s  minus startup=1.869s  flop=83.9 MFLOP
numsol matmul: 44.9 MFLOP/s
```

### What that means for the API question

Adding attention, convolution, batched rank-3 `matmul`, dropout, batch norm,
train/eval mode or checkpointing to zenith **would not help this project**,
because none of them is the constraint. A 129M-parameter model needs about
744 MFLOP per token forward+backward; at 44.9 MFLOP/s that is ~17 seconds per
token, so the 2.0B-token target run is on the order of a thousand years. The
layer catalogue is not what stands in the way — `kernel.sau`, pure Saule loops
on a register VM, is.

Both READMEs already identify this without being asked: *"`kernel.sau` is the
single file a native backend replaces."* That is exactly right.

### So the useful ask, if you want these libraries to matter computationally

A **native package implementing the kernel layer** — not new layers. Saule's
native-package system is already built for this and is unusually good: one
`.dylib`/`.so`/`.dll` dropped in `~/.saule/native_packages/`, signatures
compiled into the library and read without loading it, so `Kernel.matmul`
type-checks and completes in the editor before any of its code runs, across a
frozen C ABI.

numsol's layering is already one-way — `Kernel` and `Shape` know nothing about
`NDArray` — which the README notes is "why a native backend would replace
`kernel.sau` and leave everything else alone." A BLAS-backed `Kernel` would
plausibly buy three orders of magnitude and make zenith viable for small
on-robot models.

Worth being clear about what it would not buy: it still would not train the
GPT. That needs a GPU and PyTorch, and it is what the RX 6700 XT is for.

### Where these libraries do earn their place

1. **Corpus.** 3,059 lines of idiomatic, fully type-annotated, clean-checking
   Saule by the same author as the compiler, plus two substantial READMEs.
2. **Doc-grounded tasks.** numsol's README documents broadcasting rules,
   1-based indexing and the `*` vs `matmul` distinction — and its own "Known
   limits" section is a ready-made source of questions with correct answers.
3. **Tier 2 sensor math**, if the robot needs vectors and transforms in the
   action language.

None of those need a single new method.

### If you do extend them anyway, the gaps most worth closing for *corpus* value

Purely because they would add idiomatic examples of constructs the model
should learn, not because the project needs the features:

- numsol: integer arrays (currently float-only), rank-3+ `matmul`, views or
  strides to avoid copying on `reshape`.
- zenith: save/load built on the existing `Linear.setWeight` and
  `Tensor.setValue` hooks — this one is also just good practice.

---

## Not gaps — things that already work well

Recorded so none of this gets re-litigated later:

- **`saule fmt` is a sound canonicaliser.** Verified idempotent on all 97
  fixtures. Only 4 of the 97 are already canonical, so the training corpus
  will differ textually from the repo as committed — expected, not a problem.
- **`saule check` is fast enough to be the gate.** ~4 ms per file at 8-way
  parallelism, ~15,000 samples/min, measured on an M1 with one process spawn
  per file. Scoring is not the bottleneck anyone expected it to be.
- **`tests/ui/` is an excellent seed set**: 157 hand-written, compiler-verified
  broken programs, each pinning one diagnostic, with 156 recorded outputs.
- **Exact-output grading already exists.** 97 fixtures paired 1:1 with
  `expected/*.out` capturing stdout and stderr together, diagnostics included,
  with `SAULE_BLESS=1` to regenerate. This is a better eval harness than most
  projects build on purpose.
- **The native-package ABI** is genuinely well designed — see gap 4.

## One operational hazard, not a language gap

The `saule` on `PATH` was `26.1-dev+8c0eddf` while the repository HEAD was
`9ce2dbb`, and they disagreed: `examples/markdown` and `examples/md-viewer`
exited non-zero under the installed binary and clean under HEAD. A reward
signal is only meaningful next to the exact compiler that issued it, so the
harness pins `data.saule.binary` and `data.saule.git_sha` and records both in
every manifest. Worth a note in the Saule README that `cargo install` leaves a
binary that can silently lag the tree.

Relatedly: `wasm-pack` is not installed, and the checked-in
`www/src/lib/saule_wasm/saule_wasm_bg.wasm` is from 2026-09-30, predating
HEAD. The playground is therefore running an older compiler than the repo.
