#!/usr/bin/env python3
"""Turn a Claude Code session export into a readable Markdown transcript.

The raw export is JSON Lines and includes base64 screenshot payloads, which
make it both enormous and unreadable. This produces two artefacts:

* ``transcript.jsonl`` -- the complete record with base64 image blobs replaced
  by a short placeholder. Every word of the conversation is preserved; only
  the binary payloads are dropped.
* ``conversation.md``  -- the human-readable version: prompts, replies, and a
  one-line summary of every tool call, in order.

Usage:
    python render_transcript.py <session-export.zip | transcript.jsonl> [outdir]
"""

from __future__ import annotations

import json
import re
import sys
import zipfile
from datetime import datetime
from pathlib import Path

MAX_TOOL_OUTPUT = 700
MAX_BLOB = 4000

BASE64_RE = re.compile(r"^[A-Za-z0-9+/=\s]{%d,}$" % MAX_BLOB)

#: The repository is public, so personal identifiers are replaced before the
#: history is committed. Nothing about the engineering discussion is touched --
#: only an email address, the Windows home directory, and anything shaped like
#: a credential.
REDACTIONS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "<redacted-email>"),
    # Covers every spelling the session produced: Windows (C:\Users\name),
    # Git Bash (/c/Users/name) and the slugged cache directory
    # (C--Users-name-Documents-...).
    (re.compile(r"(?i)(users[\\/_-]+)kafka"), r"\1<user>"),
    # Also appears bare in `ls -l` owner columns. This project never discusses
    # Apache Kafka, so the standalone token is always the account name.
    (re.compile(r"(?i)\bkafka\b"), "<user>"),
    (re.compile(r"\b(gh[pousr]_[A-Za-z0-9*]{10,})"), "<redacted-token>"),
    (re.compile(r"\b(sk-[A-Za-z0-9]{12,})"), "<redacted-token>"),
]


def redact(text: str) -> str:
    for pattern, replacement in REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


def strip_blobs(value):
    """Recursively replace base64 payloads with a placeholder."""
    if isinstance(value, str):
        if len(value) > MAX_BLOB and BASE64_RE.match(value):
            return f"<<base64 payload removed: {len(value)} chars>>"
        return redact(value)
    if isinstance(value, list):
        return [strip_blobs(v) for v in value]
    if isinstance(value, dict):
        return {k: strip_blobs(v) for k, v in value.items()}
    return value


def load_records(source: Path) -> list[dict]:
    if source.suffix == ".zip":
        with zipfile.ZipFile(source) as archive:
            name = next(n for n in archive.namelist() if n.endswith("transcript.jsonl"))
            raw = archive.read(name).decode("utf-8", errors="replace")
    else:
        raw = source.read_text(encoding="utf-8", errors="replace")

    records = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def blocks_of(message) -> list[dict]:
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    return []


def clean_user_text(text: str) -> tuple[str, list[str]]:
    """Split a user turn into what the person typed and the harness's notes."""
    notes = re.findall(r"<system-reminder>(.*?)</system-reminder>", text, re.S)
    spoken = re.sub(r"<system-reminder>.*?</system-reminder>", "", text, flags=re.S)
    return spoken.strip(), [n.strip() for n in notes]


def describe_tool(block: dict) -> str:
    name = block.get("name", "tool")
    args = block.get("input") or {}
    for key in ("file_path", "command", "description", "pattern", "url", "query", "skill"):
        if key in args:
            value = str(args[key]).replace("\n", " ")
            if len(value) > 150:
                value = value[:150] + "…"
            return f"**{name}** — {value}"
    return f"**{name}**"


def timestamp(record: dict) -> str:
    raw = record.get("timestamp")
    if not raw:
        return ""
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return str(raw)


def render(records: list[dict]) -> str:
    lines: list[str] = [
        "# Full AI chat history",
        "",
        "Rendered from the Claude Code session export by `render_transcript.py`.",
        "Tool outputs are truncated; the complete record is in `transcript.jsonl`.",
        "",
        "---",
        "",
    ]

    turn = 0
    for record in records:
        kind = record.get("type")
        if kind not in {"user", "assistant"}:
            continue
        message = record.get("message")
        if not isinstance(message, dict):
            continue

        blocks = blocks_of(message)
        if message.get("role") == "user":
            texts, results = [], []
            for block in blocks:
                if block.get("type") == "text":
                    spoken, _ = clean_user_text(block.get("text", ""))
                    if spoken:
                        texts.append(spoken)
                elif block.get("type") == "tool_result":
                    content = block.get("content")
                    if isinstance(content, list):
                        content = " ".join(
                            c.get("text", "") for c in content if isinstance(c, dict)
                        )
                    content = str(content or "").strip()
                    if content:
                        results.append(content)
                elif block.get("type") == "image":
                    results.append("[image]")

            if texts:
                turn += 1
                lines.append(f"## Turn {turn} · Kaushal · {timestamp(record)}")
                lines.append("")
                lines.extend(["> " + l for t in texts for l in t.splitlines()])
                lines.append("")
            for result in results:
                if len(result) > MAX_TOOL_OUTPUT:
                    result = result[:MAX_TOOL_OUTPUT] + f"\n… [{len(result)} chars total]"
                lines.append("<details><summary>tool result</summary>")
                lines.append("")
                lines.append("```")
                lines.append(result)
                lines.append("```")
                lines.append("</details>")
                lines.append("")
        else:
            said, calls = [], []
            for block in blocks:
                if block.get("type") == "text" and block.get("text", "").strip():
                    said.append(block["text"].strip())
                elif block.get("type") == "tool_use":
                    calls.append(describe_tool(block))
            if said or calls:
                lines.append(f"### Claude · {timestamp(record)}")
                lines.append("")
                for text in said:
                    lines.append(text)
                    lines.append("")
                for call in calls:
                    lines.append(f"- {call}")
                if calls:
                    lines.append("")

    return "\n".join(lines)


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(1)

    source = Path(sys.argv[1])
    out_dir = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(".")
    out_dir.mkdir(parents=True, exist_ok=True)

    records = load_records(source)
    print(f"{len(records)} records read from {source}")

    slim = [strip_blobs(r) for r in records]
    jsonl = out_dir / "transcript.jsonl"
    with jsonl.open("w", encoding="utf-8") as stream:
        for record in slim:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(f"{jsonl}  ({jsonl.stat().st_size / 1_000_000:.1f} MB)")

    markdown = out_dir / "conversation.md"
    markdown.write_text(render(slim), encoding="utf-8")
    print(f"{markdown}  ({markdown.stat().st_size / 1_000:.0f} KB)")


if __name__ == "__main__":
    main()
