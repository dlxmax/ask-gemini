---
name: ask-gemini
description: Send a prompt to any Google Gemini model through the Gemini Interactions API and get the reply back, for a second opinion or a task Gemini does better. A main use is polishing non-English writing, especially Korean, into native-quality prose, since Claude's Korean tends to sound translated or stiff. Use this whenever you write, translate, or edit Korean (or Japanese, Chinese, or other non-English text) that a person will read (emails, messages, docs, UI strings, worksheets), even if the user doesn't mention Gemini. Also use it to probe prompt wording for a Gemini generating call, a Decisions question (gemini-decision-1) or an embedding setup on a few cases before any bench (probe.py), and when the user says "ask Gemini", "run it by Gemini", "what does Gemini think", or names a Gemini model. Not for writing code that calls the Gemini API (that's gemini-interactions-api).
---

# ask-gemini

A small CLI, `scripts/ask_gemini.py` (in this skill's directory), sends a prompt to a Gemini model and prints the reply. Use it as a tool: you stay in charge, Gemini does one job, and you check its result before using it.

## Running it

Run with `uv`, which installs the `google-genai` dependency automatically:

```bash
uv run -q --script <skill-dir>/scripts/ask_gemini.py [options] "prompt"
```

Without `uv`, use `python3 <skill-dir>/scripts/ask_gemini.py` after `pip install -U 'google-genai>=2.3.0'`.

The prompt can be an argument, a file (`-f draft.md`), or stdin. For anything longer than a line, or anything containing quotes or `$`, use a file or a quoted heredoc (`<<'EOF'`) so the shell doesn't mangle it.

The reply goes to stdout. A line with the model and `interaction_id` goes to stderr. `--json` prints `{model, interaction_id, text}` instead.

| Option | Use |
|---|---|
| `-m MODEL` | Model ID. Default `gemini-3.5-flash-lite` (override with env `ASK_GEMINI_MODEL`). For harder writing or reasoning, use a stronger model such as `gemini-3.6-flash` or `gemini-3.1-pro-preview`. If unsure which IDs are current, check the gemini-interactions-api skill. |
| `--polish LANG` | Rewrite the input as native-quality prose in LANG. See below. |
| `--register TEXT` | With `--polish`: target tone. Korean defaults to 해요체 (casual-polite). |
| `--notes TEXT` | With `--polish`: audience, terms to keep, length limits, etc. |
| `--fallback MODEL` / `--no-fallback` | Fallback chain, tried when the main model is out of quota or overloaded. Default `gemini-3.1-flash-lite` (env `ASK_GEMINI_FALLBACKS`). Use `--no-fallback` when the user asked for a specific model and a different one won't do. |
| `-s TEXT` / `--system-file F` | System instruction for free-form requests. |
| `--thinking LEVEL` | `low`/`medium`/`high` (default `medium`, or set `ASK_GEMINI_THINKING`) |
| `--previous-interaction-id ID` | Continue a conversation (stays on `--model`, no fallback). The server keeps the earlier turns, but system instructions apply to one turn only, so pass them again. |
| `--no-store` | Don't store the interaction on Google's servers. This also disables follow-ups. |

## Polishing non-English writing

This is the main use. Claude's Korean is usually correct but sounds translated: calques, stiff 것이다 constructions, mixed politeness levels, odd word choice. Gemini writes more natural Korean, so:

1. Write the content yourself first, in the target language. You know the context, the facts, and what the user needs, and Gemini doesn't.
2. Save the draft to a file and run:
   ```bash
   uv run -q --script <skill-dir>/scripts/ask_gemini.py --polish Korean -f draft.md
   ```
   Add `--register` if the user wants something other than 해요체 (for example `"합쇼체, formal business"` or `"반말, close friends"`). Use `--notes` for audience and constraints (`"for 7th-grade students"`, `"keep 'API' in English"`).
3. Read the result before using it. Check that the meaning, numbers, names, links, and formatting all survived, and that nothing was added. If Gemini changed a fact, put the correct fact back in and keep the rest of its wording.
4. Give the user the polished text. Say briefly that Gemini polished it, so they know where the wording came from.

You can also polish an English draft straight into another language: the preset handles translation and rewriting in one step. For short text that has to be exact (legal wording, quoted UI labels), consider showing the user both versions.

## Long or structured documents

For Markdown docs, READMEs and anything over a page, "read the result" isn't enough: at this size Gemini (especially flash-lite) makes mistakes that look fine at a glance. Seen in practice: bold broken by a particle glued after `**`, spacing fixes it was asked for but skipped, and CRLF line endings dropped.

1. Polish one file per call. Pass the constraints in `--notes`, for example: `"Edit only the Korean. Leave headings byte-for-byte unchanged (TOC anchors depend on them). Same number of lines, same order. Keep code, URLs, link targets, numbers, <br> and trailing two-space line breaks. Output the whole file with no code fences."`
2. Save the output to a new file, never over the original.
3. Check it mechanically:
   ```bash
   uv run -q --script <skill-dir>/scripts/check_polish.py --fix --korean orig.md polished.md
   ```
   `--fix` strips a wrapping code fence and restores the original line endings and final newline. The script then reports line-count changes, edited headings or non-Korean lines, lost trailing breaks, link targets/inline code/URLs/numbers/`<br>` that differ, literal `*` that shows up in the rendered Markdown (broken bold), and leftover spaced particles such as `v1 은` or `HA 에서` (with `--korean`). Several `orig new` pairs can go in one run. Drop `--korean` for other languages.
4. Fix every reported problem by hand in the polished file, then rerun until it's clean.
5. Review the wording diff it prints. It hides spacing-only changes, so you only read the real rewordings; check each against the original meaning.

### Korean Markdown trap: particles after bold

`**프리셋 (preset_mode)** 을` polished to `**프리셋(preset_mode)**을` renders a literal `**`: under CommonMark (and GitHub), a closing `**` right after punctuation and right before a letter isn't a closing delimiter. Write `**프리셋**(preset_mode)을` instead, or keep the space. `check_polish.py` catches this.

## Probing prompt wording (probe.py)

Use this before any bench when the question is about wording: does this rule cause the error, does a ban backfire, which question phrasing separates the cases. A handful of probe calls usually answers what three full-class runs and several review rounds did not.

```bash
uv run -q --script <skill-dir>/scripts/probe.py generate -v v1.txt -v v2.txt -c caseA.txt -c caseB.txt --grep 'REGEX' --out probe/
uv run -q --script <skill-dir>/scripts/probe.py decide   -v q1.json -v q2.json -c caseA.txt -c caseB.txt
uv run -q --script <skill-dir>/scripts/probe.py embed    --embed gemini-embedding-001:SEMANTIC_SIMILARITY --embed gemini-embedding-2:SEMANTIC_SIMILARITY -c pair1.txt
```

- `generate`: each variant is a full prompt; `{{case}}` is replaced by the case text, else the case is appended in `<case>` tags. Default 2 runs per variant x case, `--thinking medium`, `--schema F` for structured output, `--system-file F` shared by all variants. Use the production model and thinking level.
- `decide`: each variant is a Decisions `questions` JSON object; each case is the input. One run (it is deterministic). Question `type` must be `choice`, `noul` or `score` (no `yes_no`); a choice question carries `instructions` and `criteria` {option: description}. Read the probabilities: a clear case near 0.3-0.7 means the question or options are wrong, not the cutoff.
- `embed`: each case file is two texts split by a line `---`; prints cosine for each `MODEL:TASK`. For spotting a task type or model that collapses, not for setting a cutoff.
- `--grep REGEX` counts matches in every reply and totals them per variant, e.g. names a ban was meant to stop.
- The model is pinned (no fallback) and nothing is cached or stored, so variants are compared fairly. Every mode rotates across all keys in the key file and honors the optional quota hook (README, Quotas).

Size: 2-4 variants, 2-3 cases that reach the changed branch (a known failure plus a case that must not change), 2-3 runs each. When probing a ban, include a variant without it. Then bench only the winner. A probe picks wording; it never sets a cutoff, which needs a labeled set held out from the one used to pick it.

## Free-form requests

For a second opinion, a review, or anything else, send a self-contained prompt. Gemini sees nothing of this conversation, so include the needed context and files' contents:

```bash
uv run -q --script <skill-dir>/scripts/ask_gemini.py -m gemini-3.1-pro-preview -s "You are a careful reviewer." -f request.md
```

Treat the answer as input to your own judgment, not a verdict. If you disagree with it, say so and explain why.

## API keys

The script looks for a key in this order:

1. `GEMINI_API_KEY`: one key, or several separated by commas.
2. The file named by `ASK_GEMINI_KEY_FILE`, else `~/.config/ask-gemini/config.json`. It can be JSON with a `gemini_api_key`, `llm_api_key` or `api_key` field (a string or a list), or plain text with one key per line.

With several keys, the script handles rotation itself. You don't need to retry. It:
- Parks a key when that key runs out of quota for a model. A daily quota parks it until Google's reset at Pacific midnight. Parks are remembered between runs in `~/.cache/ask-gemini/`.
- Drops keys that are invalid.
- Falls back to the next model in the chain when a model is out of quota on every key, overloaded, or failing.
- Waits and retries briefly if every model is busy.

The stderr line shows which model actually answered, for example `model=gemini-3.1-flash-lite (fell back from gemini-3.5-flash-lite)`. If a fallback model answered a polish request, check its output more closely, and mention the fallback if quality matters. Never print, echo, or `cat` key files or the variable; keys must stay out of the conversation. If no key is found, tell the user to get one at https://aistudio.google.com/apikey and set it up as above.

## Errors

- `no API key`: see above.
- Model not found (404): the model ID is wrong or retired. Use a current ID from the table above or from the gemini-interactions-api skill.
- `all keys/models exhausted`: every key is parked or out of quota. The message says when the earliest one frees up. Tell the user; don't loop retrying.
- `empty reply twice`: often a safety block. Rephrase the request, or tell the user.
- `input too large`: shorten or split the input.
