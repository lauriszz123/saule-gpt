# saule-gpt

A multimodal conversational agent whose subject is [Saule](../saule) — a
statically typed, class-oriented scripting language no existing model has
seen. The model emits Saule, the typechecker gates it, and the VM runs it.

**Stage 0 of 13. Nothing is built yet.** This repository currently holds the
probe results, the decisions taken from them, and the config schema that makes
those decisions machine-checked.

## Read these first

| File | What it is |
|---|---|
| [docs/DECISIONS.md](docs/DECISIONS.md) | ADR-0001…0009, and the open questions for the Stage 0 gate |
| [docs/SAULE_TOOLCHAIN_GAPS.md](docs/SAULE_TOOLCHAIN_GAPS.md) | four measured gaps in the toolchain that block the data pipeline, each with the smallest fix |
| [docs/RUNBOOK.md](docs/RUNBOOK.md) | pinned versions and the exact commands, per machine |
| [configs/stage0.yaml](configs/stage0.yaml) | the three declared device profiles, validated |

## What Stage 0 established

- The session ran on the **M1 Air (16 GB, measured)**, not the Linux training
  box. The RX 6700 XT was never reachable, so **no Tier 0 throughput number
  here is measured** — `scripts/probe_tier0.py` exists to fix that and must be
  run before any training is approved. See ADR-0001.
- The natural Saule corpus is **353 files, 29,332 lines, ~215k tokens** with
  the eval fixtures held out. That is Chinchilla-optimal for a model of
  roughly 11,000 parameters, which confirms by measurement that transfer must
  lead and from-scratch is a control. See ADR-0008.
- `saule check` runs at **~15,000 samples/min** at 8-way parallelism, so
  compile-and-check scoring is not the bottleneck. Generation is. See ADR-0005.
- `saule disasm` is **single-file only**, so bytecode dedup covers 113/113
  single-file programs but only 14/68 example files. See ADR-0006 and gap 1.

## Reproducing the numbers

```bash
/opt/homebrew/bin/python3.14 -m venv .venv
.venv/bin/pip install 'pydantic>=2.9' 'pyyaml>=6'

.venv/bin/python scripts/corpus_census.py --roots ../saule ../saule-numsol ../saule-zenith
.venv/bin/python -c "import sys; sys.path.insert(0,'src'); \
  from config.schema import Config; print(Config.load('configs/stage0.yaml').profile_name)"
```

On the Linux side of the training machine:

```bash
python3 scripts/probe_tier0.py --json probe_tier0.json
```

## Layout

Directories arrive with the stage that fills them; the tree is not
pre-created with empty packages.

```
configs/        stage0.yaml — device profiles and toolchain pins
src/config/     schema.py — the validated single source of truth
scripts/        probe_tier0.py, corpus_census.py
docs/           DECISIONS.md, RUNBOOK.md, SAULE_TOOLCHAIN_GAPS.md
```
