# ask-gemini

A [Claude Code](https://claude.com/claude-code) skill that lets Claude call any Google Gemini model through the [Gemini Interactions API](https://ai.google.dev/gemini-api/docs/interactions) and use the reply.

The main use is **polishing non-English writing**. Claude's Korean, for example, is usually correct but often sounds translated. With this skill, Claude writes the content and then has Gemini rewrite it into natural, native-quality prose. Korean defaults to 해요체 (casual-polite), and Claude checks the result before giving it to you. You can also ask Gemini for a second opinion on anything.

## Install

**As a plugin:**

```
/plugin marketplace add dlxmax/ask-gemini
/plugin install ask-gemini@ask-gemini
```

**Or copy the skill:**

```bash
git clone https://github.com/dlxmax/ask-gemini
cp -r ask-gemini/skills/ask-gemini ~/.claude/skills/
```

**Requirements:** [`uv`](https://docs.astral.sh/uv/), which installs `google-genai` on first run. Without `uv`, use Python 3.10+ with `pip install -U 'google-genai>=2.3.0'`.

## API key

Get a key at <https://aistudio.google.com/apikey>, then do one of the following:

- `export GEMINI_API_KEY=...` (several keys can be comma-separated; the script switches to the next key when one hits its quota)
- put the key in `~/.config/ask-gemini/config.json` as `{"gemini_api_key": "..."}` (a string or a list), or as a plain-text file with one key per line
- point `ASK_GEMINI_KEY_FILE` at any file in one of those formats

## Usage

Usually you just ask Claude for Korean (or other non-English) text and the skill triggers on its own. You can also ask directly: *"run this by Gemini"* or *"have gemini-3.1-pro-preview review this plan."*

The script also works on its own:

```bash
S=~/.claude/skills/ask-gemini/scripts/ask_gemini.py
uv run -q --script $S --polish Korean -f draft.md                     # 해요체 by default
uv run -q --script $S --polish Korean --register "합쇼체, formal" -f draft.md
uv run -q --script $S --polish Japanese --notes "for a business email" -f draft.md
uv run -q --script $S -m gemini-3.1-pro-preview "Explain monads in two sentences."
uv run -q --script $S --previous-interaction-id <id> "And give an example."
```

| Option | Description |
|---|---|
| `-m MODEL` | Model ID (default `gemini-3.5-flash-lite`, or set `ASK_GEMINI_MODEL`) |
| `--polish LANG` | Rewrite the input as native-quality prose in LANG |
| `--register TEXT` | Target tone for `--polish` (Korean default: 해요체) |
| `--notes TEXT` | Extra instructions for `--polish` |
| `-s` / `--system-file` | System instruction |
| `--fallback MODEL` / `--no-fallback` | Fallback chain (default `gemini-3.1-flash-lite`, or set `ASK_GEMINI_FALLBACKS`) |
| `--thinking LEVEL` | `low`/`medium`/`high` (`--polish` defaults to `high`) |
| `--previous-interaction-id` | Continue a conversation |
| `--no-store` | Don't store the interaction server-side |
| `--json` | Print `{model, interaction_id, text}` |

## Quotas, multiple keys and errors

The script is built for free-tier keys, whose quotas apply per project and per model:

- **Quota errors** park that model-and-key pair and move on to the next key.
  - A daily quota parks it until Google's reset at midnight Pacific time.
  - A per-minute quota parks it for the delay Google advertises.
  - An unclear quota error parks it for 30 minutes from the second strike, doubling each time.
- **Overloaded, timed-out or failing models** pass the request to the next model in the fallback chain. If every model is busy, the script waits 1 s, 10 s, then 30 s between attempts.
- **Invalid keys** are skipped for the rest of the run.
- **Bad requests, oversized input and empty or blocked replies** fail right away with a clear message.

Parks are stored in `~/.cache/ask-gemini/parks.json`, or in `$ASK_GEMINI_STATE_DIR`. Keys are recorded there only as short SHA-256 fingerprints.

Two optional settings:

- **Per-day caps:** add `"llm_rpd_limits": {"flash-lite": 500}` to the config JSON, mapping a model-name substring to a request count. The script then stops using a key for that model once it hits the cap, without spending a request to find out.
- **Shared parks:** if another tool uses the same keys in the same order, set `ASK_GEMINI_EXTRA_PARKS` to its park file to respect its parks (read-only). The file format is `{"day_dead": [[model, key_index, until_epoch]], "cooldown": [...]}`.

## Privacy

Whatever you send goes to Google's Gemini API. By default the Interactions API stores interactions: 55 days on the paid tier, 1 day on the free tier. Use `--no-store` to opt out.

## License

MIT
