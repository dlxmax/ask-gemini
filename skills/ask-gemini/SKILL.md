---
name: ask-gemini
description: Send a prompt to any Google Gemini model through the Gemini Interactions API and get the reply back, for a second opinion or a task Gemini does better. A main use is polishing non-English writing, especially Korean, into native-quality prose, since Claude's Korean tends to sound translated or stiff. Use this whenever you write, translate, or edit Korean (or Japanese, Chinese, or other non-English text) that a person will read (emails, messages, docs, UI strings, worksheets), even if the user doesn't mention Gemini. Also use it to probe prompt wording for a Gemini generating call, a Decisions question (gemini-decision-1) or an embedding setup on a few cases before any bench (probe.py), and when the user says "ask Gemini", "run it by Gemini", "what does Gemini think", or names a Gemini model. Not for writing code that calls the Gemini API (that's gemini-interactions-api).
---

# ask-gemini

Run: `uv run -q --script <skill-dir>/scripts/ask_gemini.py [opts] "prompt"` (or `-f FILE`, or stdin; use a file or `<<'EOF'` for anything with quotes, `$` or newlines). Reply on stdout; stderr names the model that answered.

Options: `-m MODEL` (default `gemini-3.5-flash-lite`; harder work: `gemini-3.6-flash`, `gemini-3.1-pro-preview`; current IDs in gemini-interactions-api) · `--polish LANG` `--register TEXT` `--notes TEXT` · `-s TEXT` / `--system-file F` · `--thinking low|medium|high` · `--fallback M` / `--no-fallback` (use when the user named the model) · `--previous-interaction-id ID` (resend system instruction) · `--no-store` · `--json`.

Keys: `GEMINI_API_KEY` (comma list) or `~/.config/ask-gemini/config.json` / `$ASK_GEMINI_KEY_FILE`. Never print or cat keys. Rotation, parking, fallback and retries are built in: don't retry yourself. If a fallback model answered a polish, check harder.

## Polish

1. Write the draft yourself in the target language (or English; the preset translates).
2. `--polish Korean -f draft.md`. Korean defaults to 해요체; `--register` to change; `--notes` for audience and terms to keep.
3. Check meaning, facts, numbers, names, links, formatting; nothing added. Restore any changed fact.
4. Tell the user Gemini polished it. For exact short text (legal, UI labels), show both.

Long or Markdown docs: one file per call; `--notes` "Edit only the Korean. Headings byte-for-byte. Same lines and order. Keep code, URLs, link targets, numbers, `<br>`, trailing two-space breaks. Whole file, no fences." Save to a new file, then `check_polish.py --fix --korean orig.md polished.md`; fix every report by hand, rerun until clean, read the wording diff. Trap: `**x(y)**을` renders literal `**`; write `**x**(y)을`.

## Probe wording (probe.py)

Before any bench, when the question is wording.

```bash
probe.py generate -v v1.txt -v v2.txt -c a.txt -c b.txt [--grep RE] [--out DIR] [-m M --thinking L --schema F --system-file F]
probe.py decide   -v q1.json -v q2.json -c a.txt
probe.py embed    --embed gemini-embedding-001:SEMANTIC_SIMILARITY -c pair.txt
```

- generate: `{{case}}` in the variant takes the case, else appended in `<case>`. Default 2 runs. Match production model and thinking.
- decide: variant = questions map by id, no wrapper: `{"keep": {"type": "choice", "instructions": "...", "criteria": {"kept": "...", "lost": "..."}}}`. Type `choice|noul|score`. Case file = plain input text, no JSON (the item; for meaning checks, the source after it). Meaning checks: put the source in the case file. Clear case at P 0.3-0.7 = bad question, not bad cutoff.
- embed: case = two texts split by a `---` line; prints cosine.
- Run directly under `timeout 600`, not behind a `pgrep` wait loop (`pgrep -f` matches the loop itself). Output prints only at the end; check the process and stderr before blaming the API.
- Cheapest first: 1 run, 2 cases; probe the narrow sub-job, not the whole production prompt.
- Pinned model, no cache. Size: 2-4 variants, 2-3 cases on the changed branch (a failure + a must-not-change), 2-3 runs; probing a ban, include a variant without it. Bench only the winner. Never set a cutoff from a probe.

## Free-form

Gemini sees nothing of this conversation: send a self-contained prompt with file contents. Treat the answer as input, not a verdict.

## Errors

404: bad model ID. `exhausted`: report the free-up time, don't loop. `empty reply twice`: likely safety block, rephrase. `input too large`: split.
