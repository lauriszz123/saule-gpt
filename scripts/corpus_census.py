#!/usr/bin/env python3
"""Census of the Saule corpus: what exists, and what is safe to train on.

Every number in the Stage 0 report comes from this script, so the report can
be re-derived rather than trusted:

    python3 scripts/corpus_census.py --roots ../saule ../saule-numsol ../saule-zenith

Two things it does that a `find`-and-`wc` one-liner gets wrong, both of which
would quietly corrupt the dataset:

  * EXCLUDES `.claude/worktrees/`. Those directories are agent git worktrees
    holding complete copies of the repository, test fixtures included. On the
    tree measured at Stage 0 they contributed 503 extra `.sau` files -- more
    than the real corpus -- every one a duplicate of a file already counted.
    A naive glob trains on each fixture twice and defeats dedup.
  * EXCLUDES `target/` and `node_modules/`, which hold build output and
    vendored JavaScript, neither of which is Saule anyone wrote.

It also separates the corpus by its role in training, because the three groups
cannot be mixed:

  * `tests/*.sau` (97) -- paired 1:1 with `tests/expected/*.out`. This is the
    exact-output eval set AND a large share of a tiny corpus, so it is the
    contamination risk the whole project hinges on. Reported separately so the
    split can happen before any generator runs.
  * `tests/ui/*.sau` (157) -- deliberately broken programs, each pinning one
    diagnostic. Seeds for error-repair generation, never targets to imitate.
  * everything else -- examples, benchmarks, libraries: valid Saule to learn.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

# Directories that never contain corpus, whatever their contents look like.
# `.claude` is the dangerous one: it holds whole-repo worktrees.
EXCLUDED_DIR_NAMES = {".claude", "target", "node_modules", ".git", "dist", ".venv"}


@dataclass
class Group:
    """One role a set of files plays in training."""

    name: str
    note: str
    files: list[Path] = field(default_factory=list)
    lines: int = 0
    bytes_: int = 0
    # sha256 of the raw bytes -> how many files share it.
    hashes: Counter[str] = field(default_factory=Counter)

    def add(self, path: Path, text: str, raw: bytes) -> None:
        self.files.append(path)
        self.lines += len(text.splitlines())
        self.bytes_ += len(raw)
        self.hashes[hashlib.sha256(raw).hexdigest()] = (
            self.hashes[hashlib.sha256(raw).hexdigest()] + 1
        )

    @property
    def exact_duplicates(self) -> int:
        """Files whose bytes are identical to an earlier file in this group."""
        return sum(count - 1 for count in self.hashes.values() if count > 1)


def is_excluded(path: Path) -> bool:
    return any(part in EXCLUDED_DIR_NAMES for part in path.parts)


def classify(path: Path, root: Path) -> str:
    """Which training role this file plays, from its location."""
    try:
        rel = path.relative_to(root)
    except ValueError:
        return "other"
    parts = rel.parts
    if parts and parts[0] == "tests":
        if len(parts) >= 2 and parts[1] == "ui":
            return "tests_ui_broken"
        if len(parts) == 2:
            return "tests_eval_fixtures"
        return "tests_other"
    if parts and parts[0] == "examples":
        return "examples"
    if parts and parts[0] == "benchmarks":
        return "benchmarks"
    if parts and parts[0] == "src":
        return "library_src"
    return "other"


GROUP_NOTES = {
    "tests_eval_fixtures": "EVAL SET -- split before any generator runs; 1:1 with expected/*.out",
    "tests_ui_broken": "deliberately broken; error-repair seeds, never imitation targets",
    "examples": "multi-file projects; the most idiomatic Saule in the corpus",
    "benchmarks": "single-file programs",
    "library_src": "numsol / zenith library sources",
    "tests_other": "fixtures nested below tests/",
    "other": "unclassified",
}


def census(roots: list[Path]) -> tuple[dict[str, Group], dict[str, object]]:
    groups: dict[str, Group] = {}
    skipped_excluded = 0
    unreadable: list[str] = []

    for root in roots:
        root = root.resolve()
        if not root.is_dir():
            print(f"warning: {root} is not a directory, skipping", file=sys.stderr)
            continue
        for path in sorted(root.rglob("*.sau")):
            if is_excluded(path.relative_to(root)):
                skipped_excluded += 1
                continue
            try:
                raw = path.read_bytes()
            except OSError as exc:
                unreadable.append(f"{path}: {exc}")
                continue
            text = raw.decode("utf-8", errors="replace")
            key = classify(path, root)
            if key not in groups:
                groups[key] = Group(name=key, note=GROUP_NOTES.get(key, ""))
            groups[key].add(path, text, raw)

    meta: dict[str, object] = {
        "roots": [str(r.resolve()) for r in roots],
        "excluded_dir_names": sorted(EXCLUDED_DIR_NAMES),
        "files_skipped_as_excluded": skipped_excluded,
        "unreadable": unreadable,
    }
    return groups, meta


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--roots",
        nargs="+",
        default=["../saule", "../saule-numsol", "../saule-zenith"],
        help="repository roots to walk",
    )
    parser.add_argument("--json", metavar="PATH", help="write the raw census here")
    args = parser.parse_args()

    groups, meta = census([Path(r) for r in args.roots])

    print("=" * 78)
    print("SAULE CORPUS CENSUS")
    print("=" * 78)
    print(f"roots: {', '.join(str(r) for r in meta['roots'])}")
    print(
        f"excluded {meta['files_skipped_as_excluded']} .sau files under "
        f"{', '.join(meta['excluded_dir_names'])}"
    )
    print()
    print(f"{'group':24} {'files':>6} {'lines':>8} {'KiB':>8} {'dup':>5}  note")
    print("-" * 78)

    total_files = total_lines = total_bytes = 0
    for key in sorted(groups, key=lambda k: -groups[k].lines):
        g = groups[key]
        print(
            f"{g.name:24} {len(g.files):6} {g.lines:8} "
            f"{g.bytes_ / 1024:8.1f} {g.exact_duplicates:5}  {g.note}"
        )
        total_files += len(g.files)
        total_lines += g.lines
        total_bytes += g.bytes_

    print("-" * 78)
    print(f"{'TOTAL':24} {total_files:6} {total_lines:8} {total_bytes / 1024:8.1f}")
    print()

    # The number that actually bounds the project: tokens available to train on,
    # at a deliberately crude 1 token per 3.2 bytes. Stage 2 replaces this with
    # a measurement from the real tokenizer.
    trainable = {
        k: g for k, g in groups.items() if k not in ("tests_eval_fixtures",)
    }
    trainable_bytes = sum(g.bytes_ for g in trainable.values())
    print(
        f"trainable bytes (eval fixtures held out): {trainable_bytes / 1024:.1f} KiB\n"
        f"  crude token estimate at 3.2 bytes/token: "
        f"~{trainable_bytes / 3.2 / 1000:.0f}k tokens"
    )
    if "tests_eval_fixtures" in groups:
        g = groups["tests_eval_fixtures"]
        share = g.bytes_ / total_bytes * 100 if total_bytes else 0
        print(
            f"\nCONTAMINATION NOTE: the eval fixtures are {len(g.files)} files, "
            f"{g.lines} lines,\n  {share:.1f}% of the corpus by bytes. Splitting them "
            "before any generator\n  runs is Stage 1's first action, not a later check."
        )

    if meta["unreadable"]:
        print("\nunreadable files:")
        for item in meta["unreadable"]:
            print(f"  {item}")

    if args.json:
        payload = {
            "meta": meta,
            "groups": {
                k: {
                    "files": [str(p) for p in g.files],
                    "count": len(g.files),
                    "lines": g.lines,
                    "bytes": g.bytes_,
                    "exact_duplicates": g.exact_duplicates,
                    "note": g.note,
                }
                for k, g in groups.items()
            },
        }
        Path(args.json).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"\nraw census written to {args.json}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
