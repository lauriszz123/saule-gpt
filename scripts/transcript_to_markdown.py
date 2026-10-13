#!/usr/bin/env python3
"""Turn a CCD session transcript (JSONL) into something readable.

The export is 7.8 MB, nearly all of it tool inputs and outputs. What is worth
reading on another machine is the conversation: what was asked, what was
answered, and which tools ran in between. So tool payloads are reduced to one
line each — the tool's name and a short label — and the prose is kept whole.

    python3 to_markdown.py transcript.jsonl > CONVERSATION.md
"""

from __future__ import annotations

import json
import sys


def text_of(content) -> tuple[str, list[str]]:
    """Prose from a message's content, plus a line per tool use."""
    if isinstance(content, str):
        return content, []
    if not isinstance(content, list):
        return "", []

    prose: list[str] = []
    tools: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            prose.append(block.get("text", ""))
        elif kind == "thinking":
            # Deliberately dropped: it is long, and it is not the record of
            # what was decided — the prose and the tool calls are.
            continue
        elif kind == "tool_use":
            name = block.get("name", "?")
            args = block.get("input", {}) or {}
            label = (
                args.get("description")
                or args.get("file_path")
                or args.get("command")
                or args.get("title")
                or ""
            )
            if isinstance(label, str) and len(label) > 110:
                label = label[:107] + "..."
            label = str(label).replace("\n", " ")
            tools.append(f"`{name}`" + (f" — {label}" if label else ""))
        elif kind == "tool_result":
            # The outputs are the bulk of the file and are reconstructible by
            # re-running the commands; the decisions made from them are in the
            # prose that follows.
            continue
    return "\n".join(p for p in prose if p.strip()), tools


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else "transcript.jsonl"

    # The export interleaves bookkeeping records with the conversation, and a
    # few lines are truncated mid-string; both are skipped rather than fatal.
    rows = []
    skipped = 0
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                skipped += 1

    out: list[str] = []
    out.append("# Conversation transcript")
    out.append("")
    out.append(
        "Exported from the Claude Code session that built the Saule toolchain "
        "work, the native numsol and the toy GPT. Prose is verbatim; each tool "
        "call is reduced to one line, and tool output is omitted — the commands "
        "can be re-run, and the conclusions drawn from them are in the prose."
    )
    out.append("")

    turn = 0
    for row in rows:
        kind = row.get("type")
        if kind not in ("user", "assistant"):
            continue
        message = row.get("message") or {}
        content = message.get("content", row.get("content"))
        prose, tools = text_of(content)
        if not prose and not tools:
            continue

        if kind == "user":
            # Harness-injected reminders are not things the person said.
            if prose.lstrip().startswith("<system-reminder>"):
                continue
            turn += 1
            out.append(f"\n---\n\n## {turn}. Laurynas\n")
            if prose:
                out.append(prose)
        else:
            out.append("\n### Claude\n")
            if prose:
                out.append(prose)
            if tools:
                out.append("\n<details><summary>"
                           f"{len(tools)} tool call(s)</summary>\n")
                for t in tools:
                    out.append(f"- {t}")
                out.append("\n</details>")

    text = "\n".join(out) + "\n"
    sys.stdout.write(text)
    print(
        f"\n<!-- {len(rows)} records read, {skipped} unparseable, {turn} turns -->",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
