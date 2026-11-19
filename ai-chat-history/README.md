# Full AI chat history

The complete session in which this project was built, from the very first
prompt.

| File | What it is |
|---|---|
| `conversation.md` | Readable transcript: every prompt, every reply, every tool call in order, with tool output truncated |
| `transcript.jsonl` | The complete raw export, one JSON record per line — nothing summarised |
| `render_transcript.py` | The script that produced both from the session export |

## How it was produced

Exported from Claude Code (session menu → Export), then:

```bash
python render_transcript.py session-export.zip .
```

## What was changed, and what was not

Two mechanical transformations, both performed by the script above:

1. **Base64 image payloads removed.** Screenshots taken during browser testing
   were embedded as base64 and made the export 8 MB of unreadable text. Each is
   replaced with `<<base64 payload removed: N chars>>`. No words were lost.
2. **Personal identifiers redacted**, because this repository is public: an
   email address, the Windows account name and home directory, and anything
   shaped like an API token. The redaction rules are at the top of
   `render_transcript.py` and are deliberately readable so you can see exactly
   what was substituted.

Nothing about the engineering discussion was edited, shortened, reordered or
cleaned up — including the parts where the first approach was wrong. The
sequence where a colour metric was found to return "completely different" for
two *identical* image patches is in there in full, which is rather the point.

## Reading it

`AI_COLLAB.md` in the repository root is the summary: the decisions that
mattered, with pointers into this history. Start there. This folder is the
evidence behind it.
