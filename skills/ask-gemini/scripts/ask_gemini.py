#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["google-genai>=2.3.0"]
# ///
"""Send a prompt to a Gemini model through the Interactions API and print the reply.

Reply text goes to stdout; a metadata line (model used, interaction id) goes to stderr,
so the id can be passed back with --previous-interaction-id for a follow-up turn.

Keys (first hit wins):
  1. GEMINI_API_KEY  (one key, or several separated by commas)
  2. ASK_GEMINI_KEY_FILE, else ~/.config/ask-gemini/config.json
     - JSON: first of "gemini_api_key", "llm_api_key", "api_key" (string or list);
       an optional "llm_rpd_limits" {model-substring: requests/day} enables proactive parking
     - otherwise plain text, one key per line

Resilience (free-tier quotas are per project *and* per model, so state is per (model, key)):
  - quota 429 -> the key is blamed: park (model, key), try the same model on another key
      * daily quota  -> parked until 00:00 America/Los_Angeles (when Google resets)
      * per-minute   -> parked for the advertised delay, else a 1-5 s jitter
      * ambiguous    -> 2nd consecutive strike parks 30 min, doubling, capped at Pacific midnight
  - 503 / overloaded / timeout -> the model is blamed: step down the model chain on the same key;
      if every model is busy, wait (1 s, 10 s, 30 s ...) and go round again
  - 500 -> try the next model; the 2nd 500 on a (model, key) parks it for 5 min
  - invalid / unauthorized key -> drop that key for this run
  - other 400-class errors, oversize input, blocked replies -> fatal, no retry
Set ASK_GEMINI_EXTRA_PARKS to also honor (read-only) another tool's park file for the same keys.
Park state lives in $ASK_GEMINI_STATE_DIR (default ~/.cache/ask-gemini/parks.json); keys are
stored there only as short SHA-256 fingerprints.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
import random
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import quota_hook  # noqa: E402

DEFAULT_MODEL = os.environ.get("ASK_GEMINI_MODEL", "gemini-3.5-flash-lite")
DEFAULT_FALLBACKS = [m for m in os.environ.get("ASK_GEMINI_FALLBACKS", "gemini-3.1-flash-lite").split(",") if m.strip()]
PRESET_DIR = Path(__file__).resolve().parent.parent / "presets"
DEFAULT_KEY_FILE = Path.home() / ".config" / "ask-gemini" / "config.json"
STATE_DIR = Path(os.environ.get("ASK_GEMINI_STATE_DIR", Path.home() / ".cache" / "ask-gemini")).expanduser()
# Optional read-only park files from another tool sharing the same key list, comma-separated.
# Format: {"day_dead": [[model, key_index, until_epoch], ...], "cooldown": [[...], ...]}
EXTRA_PARKS = [Path(x).expanduser() for x in os.environ.get("ASK_GEMINI_EXTRA_PARKS", "").split(",") if x.strip()]
JSON_KEY_FIELDS = ("gemini_api_key", "llm_api_key", "api_key")

# Default register when --polish is used without --register.
_KO_REGISTER = "해요체 (casual-polite: sentence endings -아요/-어요/-요, not 합니다체 and not 반말)"
DEFAULT_REGISTERS = {"korean": _KO_REGISTER, "ko": _KO_REGISTER, "한국어": _KO_REGISTER}

OVERLOAD_WAITS = [1, 10, 30]          # seconds between full rounds when every model is busy
MAX_ROUNDS = 4
AMBIG_BASE_PARK = 30 * 60
REPEATED_500_PARK = 300

# --- error classification ----------------------------------------------------------------

KEY_FATAL = ("api_key_invalid", "api key not valid", "api_key_expired", "permission_denied",
             "unauthenticated", "forbidden", "api key expired", "consumer_suspended")
QUOTA = ("too_many_requests", "resource_exhausted", "quota", "rate limit", "rate_limit")
OVERLOAD = ("unavailable", "overloaded", "high demand", "try again later", "temporarily",
            "is currently experiencing", "deadline_exceeded", "gateway_timeout", "request_timeout",
            "timed out", "timeout")
OVERSIZE = ("input token count", "exceeds the maximum", "context length", "too many tokens",
            "request payload size", "token limit")
SERVER = ("internal", "server_error", "backend_error")
HARD = ("invalid_argument", "bad_request", "failed_precondition", "not_found", "unsupported",
        "unimplemented")


class Fatal(Exception):
    pass


def classify(status: int | None, text: str) -> str:
    t = text.lower()
    if any(p in t for p in KEY_FATAL) or status in (401, 403):
        return "key"
    if status == 429 or any(p in t for p in QUOTA):
        return "quota"
    if any(p in t for p in OVERSIZE):
        return "oversize"
    if status in (503, 504) or any(p in t for p in OVERLOAD):
        return "overload"
    if status == 500 or any(p in t for p in SERVER):
        return "server"
    if (status and 400 <= status < 500) or any(p in t for p in HARD):
        return "hard"
    if status is None:  # connection-level failure with no HTTP status
        return "overload"
    return "server"


def quota_scope(text: str) -> str:
    """'minute', 'day', or 'unknown'. PerMinute wins: Google co-reports PerDay on rolling throttles."""
    if "perminute" in text.lower().replace("_", "").replace(" ", ""):
        return "minute"
    t = text.lower()
    if "quota_exceeded" in t or "per day" in t or "perday" in t.replace(" ", ""):
        return "day"
    return "unknown"


def retry_delay(text: str) -> float | None:
    m = re.search(r"retry(?:Delay)?['\"]?\s*[:=]?\s*['\"]?(\d+(?:\.\d+)?)s", text, re.I) or \
        re.search(r"retry in (\d+(?:\.\d+)?)\s*s", text, re.I)
    return float(m.group(1)) if m else None


# --- Pacific-time helpers ------------------------------------------------------------------

def pacific_now() -> dt.datetime:
    try:
        from zoneinfo import ZoneInfo
        return dt.datetime.now(ZoneInfo("America/Los_Angeles"))
    except Exception:  # no tzdata: fixed UTC-8 errs toward parking longer
        return dt.datetime.now(dt.timezone(dt.timedelta(hours=-8)))


def seconds_until_pacific_midnight() -> float:
    now = pacific_now()
    nxt = (now + dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(60.0, (nxt - now).total_seconds())


# --- persistent park state -----------------------------------------------------------------

class State:
    """{"parks": {"model|fp": until_epoch}, "ambig": {"model|fp": strikes},
        "e500": {"model|fp": strikes}, "rpd": {"date|model|fp": count}}"""

    def __init__(self) -> None:
        self.path = STATE_DIR / "parks.json"
        self.data: dict = {"parks": {}, "ambig": {}, "e500": {}, "rpd": {}}
        self.extra: dict = {}
        with contextlib.suppress(Exception):
            loaded = json.loads(self.path.read_text())
            for k in self.data:
                if isinstance(loaded.get(k), dict):
                    self.data[k] = loaded[k]
        now = time.time()
        self.data["parks"] = {k: v for k, v in self.data["parks"].items() if v > now}
        today = pacific_now().date().isoformat()
        self.data["rpd"] = {k: v for k, v in self.data["rpd"].items() if k.startswith(today)}

    def save(self) -> None:
        with contextlib.suppress(Exception):
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            lock = open(STATE_DIR / ".lock", "w")
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:  # merge with whatever a concurrent process wrote, then replace atomically
                with contextlib.suppress(Exception):
                    disk = json.loads(self.path.read_text())
                    for sect in ("parks", "rpd"):
                        for k, v in disk.get(sect, {}).items():
                            self.data[sect][k] = max(v, self.data[sect].get(k, 0))
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(json.dumps(self.data))
                os.replace(tmp, self.path)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
                lock.close()

    @staticmethod
    def k(model: str, fp: str) -> str:
        return f"{model}|{fp}"

    def load_extra(self, fps: list[str]) -> None:
        """Fold in another tool's parks (indexed by key position) without ever writing to them."""
        now = time.time()
        for path in EXTRA_PARKS:
            try:
                ext = json.loads(path.read_text())
            except Exception:  # missing/unmounted/corrupt: ignore, it's advisory
                continue
            for sect in ("day_dead", "cooldown"):
                for row in ext.get(sect, []) if isinstance(ext, dict) else []:
                    with contextlib.suppress(Exception):
                        model, idx, until = row[0], int(row[1]), float(row[2])
                        if until > now and 0 <= idx < len(fps):
                            self.extra[self.k(model, fps[idx])] = max(until, self.extra.get(self.k(model, fps[idx]), 0))

    def parked_for(self, model: str, fp: str) -> float:
        key = self.k(model, fp)
        return max(0.0, max(self.data["parks"].get(key, 0), self.extra.get(key, 0)) - time.time())

    def park(self, model: str, fp: str, seconds: float) -> None:
        until = time.time() + seconds
        key = self.k(model, fp)
        self.data["parks"][key] = max(until, self.data["parks"].get(key, 0))

    def strike(self, sect: str, model: str, fp: str) -> int:
        key = self.k(model, fp)
        self.data[sect][key] = self.data[sect].get(key, 0) + 1
        return self.data[sect][key]

    def clear(self, model: str, fp: str) -> None:
        for sect in ("ambig", "e500"):
            self.data[sect].pop(self.k(model, fp), None)

    def rpd_count(self, model: str, fp: str, bump: bool = False) -> int:
        key = f"{pacific_now().date().isoformat()}|{model}|{fp}"
        if bump:
            self.data["rpd"][key] = self.data["rpd"].get(key, 0) + 1
        return self.data["rpd"].get(key, 0)


# --- config / keys -------------------------------------------------------------------------

def die(msg: str, code: int = 1) -> None:
    print(f"ask-gemini: {msg}", file=sys.stderr)
    sys.exit(code)


def load_config() -> tuple[list[str], dict]:
    """Returns (keys, rpd_limits) and offers the keys and config to the optional quota hook."""
    keys, limits, data = _load_keys()
    quota_hook.enable(keys, data if isinstance(data, dict) else {})
    return keys, limits


def _load_keys() -> tuple[list[str], dict, object]:
    env = os.environ.get("GEMINI_API_KEY", "").strip()
    path = Path(os.environ.get("ASK_GEMINI_KEY_FILE", DEFAULT_KEY_FILE)).expanduser()
    data = None
    if path.is_file():
        text = path.read_text(encoding="utf-8")
        with contextlib.suppress(json.JSONDecodeError):
            data = json.loads(text)
    limits = data.get("llm_rpd_limits", {}) if isinstance(data, dict) else {}
    if env:
        return [k.strip() for k in env.split(",") if k.strip()], limits, data
    if not path.is_file():
        die("no API key. Set GEMINI_API_KEY, or point ASK_GEMINI_KEY_FILE at a key file "
            f"(default {DEFAULT_KEY_FILE}). Get a key at https://aistudio.google.com/apikey")
    if isinstance(data, dict):
        for field in JSON_KEY_FIELDS:
            val = data.get(field)
            if isinstance(val, str) and val.strip():
                return [val.strip()], limits, data
            if isinstance(val, list):
                keys = [k.strip() for k in val if isinstance(k, str) and k.strip()]
                if keys:
                    return keys, limits, data
        die(f"{path} is JSON but has none of the fields {', '.join(JSON_KEY_FIELDS)}")
    keys = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    if not keys:
        die(f"{path} contains no keys")
    return keys, limits, data


def rpd_limit(model: str, limits: dict) -> int | None:
    """Longest matching substring wins, so 'flash-lite' beats '-flash'."""
    hits = [(len(s), n) for s, n in limits.items() if s in model and isinstance(n, int)]
    return max(hits)[1] if hits else None


def fingerprint(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:12]


def build_polish_system(language: str, register: str | None, notes: str | None) -> str:
    template = (PRESET_DIR / "polish.md").read_text(encoding="utf-8")
    if not register:
        register = DEFAULT_REGISTERS.get(language.strip().lower())
    register_line = (
        f"Use this register/tone throughout, even where the draft uses another one: {register}."
        if register
        else "Keep the register and tone of the draft unless it is clearly wrong for the context."
    )
    out = template.replace("{language}", language).replace("{register_line}", register_line)
    if language.strip().lower() in ("korean", "ko", "한국어"):
        out += "\n\n" + (PRESET_DIR / "polish-korean.md").read_text(encoding="utf-8")
    if notes:
        out += f"\n\nAdditional instructions from the requester:\n{notes}"
    return out


def log(msg: str) -> None:
    print(f"ask-gemini: {msg}", file=sys.stderr)


# --- the call loop -------------------------------------------------------------------------

def call(genai, keys: list[str], chain: list[str], kwargs: dict, limits: dict):
    """Returns (interaction, model). Raises Fatal."""
    state = State()
    fps = [fingerprint(k) for k in keys]
    state.load_extra(fps)
    dead_keys: set[int] = set()
    start = random.randrange(len(keys))  # spread load across keys between invocations
    key_order = [(start + i) % len(keys) for i in range(len(keys))]
    last_err = "no attempts made"

    def usable(m: str, i: int) -> bool:
        if i in dead_keys or state.parked_for(m, fps[i]) > 0 or quota_hook.parked(m, i):
            return False
        lim = rpd_limit(m, limits)
        return not (lim and state.rpd_count(m, fps[i]) >= lim)

    try:
        for rnd in range(MAX_ROUNDS):
            busy = False
            for model in chain:  # model-major: exhaust every key on a model before falling back
                for i in key_order:
                    if not usable(model, i):
                        continue
                    fp = fps[i]
                    kind, text = None, ""
                    for attempt in (1, 2):  # second attempt only after an empty reply
                        try:
                            quota_hook.before_call(model, i, keys[i])
                            client = genai.Client(api_key=keys[i])  # keep a ref; temporaries get closed
                            inter = client.interactions.create(model=model, **kwargs)
                            state.rpd_count(model, fp, bump=True)
                            err = getattr(inter, "error", None)
                            if err or getattr(inter, "status", None) == "failed":
                                raise RuntimeError(f"in-response error: {err or inter.status}")
                            if not (inter.output_text or "").strip():
                                if attempt == 1:
                                    continue
                                raise Fatal(f"{model} returned an empty reply twice (possibly a safety "
                                            "block); rephrase the request or try another model")
                            state.clear(model, fp)
                            return inter, model
                        except Fatal:
                            raise
                        except Exception as e:  # noqa: BLE001
                            text = str(e)
                            for k in keys:
                                text = text.replace(k, "<redacted>")
                            status = getattr(e, "status_code", None)
                            try:
                                status = int(status) if status is not None else None
                            except (TypeError, ValueError):
                                status = None
                            kind = classify(status, f"{type(e).__name__} {text}")
                            last_err = f"{model} key#{i + 1}: {kind}: {text[:300]}"
                            break
                    log(f"{model} key#{i + 1}: {kind}")
                    if kind == "key":
                        dead_keys.add(i)
                        continue  # same model, next key
                    if kind == "quota":  # blame the key: park this (model, key), try the next key
                        quota_hook.note_429(model, i, text)
                        scope = quota_scope(text)
                        if scope == "day":
                            state.park(model, fp, seconds_until_pacific_midnight())
                        elif scope == "minute":
                            state.park(model, fp, retry_delay(text) or random.uniform(1, 5))
                        else:
                            n = state.strike("ambig", model, fp)
                            state.park(model, fp, random.uniform(1, 5) if n < 2 else
                                       min(AMBIG_BASE_PARK * 2 ** (n - 2), seconds_until_pacific_midnight()))
                        continue
                    if kind in ("hard", "oversize"):
                        hint = " (input too large: shorten it)" if kind == "oversize" else ""
                        raise Fatal(f"{last_err}{hint}")
                    if kind == "server":
                        if state.strike("e500", model, fp) >= 2:
                            state.park(model, fp, min(REPEATED_500_PARK, seconds_until_pacific_midnight()))
                    else:
                        state.clear(model, fp)
                    busy = True
                    break  # overload/server: blame the model, fall to the next model in the chain
            if not busy:
                break  # everything quota-parked or dead: waiting a few seconds won't help
            if rnd < MAX_ROUNDS - 1:
                wait = OVERLOAD_WAITS[min(rnd, len(OVERLOAD_WAITS) - 1)]
                log(f"models busy; waiting {wait}s (round {rnd + 2}/{MAX_ROUNDS})")
                time.sleep(wait)
    finally:
        state.save()

    soonest = min((state.parked_for(m, fps[i]) for m in chain for i in range(len(keys))
                   if i not in dead_keys and state.parked_for(m, fps[i]) > 0), default=0)
    extra = f"; earliest key/model frees up in {int(soonest // 60)} min" if soonest else ""
    raise Fatal(f"all keys/models exhausted or failing ({len(dead_keys)} invalid key(s)){extra}. "
                f"Last error: {last_err}")


def main() -> None:
    p = argparse.ArgumentParser(description="Ask a Gemini model via the Interactions API.")
    p.add_argument("prompt", nargs="?", help="Prompt text. Omit to read from stdin or --file.")
    p.add_argument("-f", "--file", help="Read the prompt from this file.")
    p.add_argument("-m", "--model", default=DEFAULT_MODEL, help=f"Model ID (default {DEFAULT_MODEL}).")
    p.add_argument("--fallback", action="append",
                   help=f"Fallback model, repeatable (default: {','.join(DEFAULT_FALLBACKS) or 'none'}).")
    p.add_argument("--no-fallback", action="store_true", help="Use only --model, never a fallback.")
    p.add_argument("-s", "--system", help="System instruction text.")
    p.add_argument("--system-file", help="Read the system instruction from this file.")
    p.add_argument("--polish", metavar="LANGUAGE",
                   help="Preset: rewrite the input as native-quality prose in LANGUAGE (e.g. Korean).")
    p.add_argument("--register", help="With --polish: target register/tone (Korean default: 해요체).")
    p.add_argument("--notes", help="With --polish: extra instructions (audience, terms to keep, etc.).")
    p.add_argument("--thinking", choices=["low", "medium", "high"],
                   default=os.environ.get("ASK_GEMINI_THINKING", "medium"),
                   help="thinking_level (default medium, or env ASK_GEMINI_THINKING).")
    p.add_argument("--previous-interaction-id",
                   help="Continue an earlier interaction (pins to --model; no fallback).")
    p.add_argument("--no-store", action="store_true", help="Don't store the interaction server-side.")
    p.add_argument("--json", action="store_true", help="Print {model, interaction_id, text} as JSON.")
    args = p.parse_args()

    if args.file:
        prompt = Path(args.file).read_text(encoding="utf-8")
    elif args.prompt is not None:
        prompt = args.prompt
    elif not sys.stdin.isatty():
        prompt = sys.stdin.read()
    else:
        die("no prompt given (pass text, --file, or pipe stdin)", 2)
    if not prompt.strip():
        die("prompt is empty", 2)

    system = build_polish_system(args.polish, args.register, args.notes) if args.polish else None
    for extra in (Path(args.system_file).read_text(encoding="utf-8") if args.system_file else None, args.system):
        if extra:
            system = (system + "\n\n" if system else "") + extra

    try:
        from google import genai
    except ImportError:
        die("google-genai not installed. Run with `uv run` or `pip install -U 'google-genai>=2.3.0'`.")

    kwargs: dict = {"input": prompt}
    if system:
        kwargs["system_instruction"] = system
    kwargs["generation_config"] = {"thinking_level": args.thinking}
    if args.previous_interaction_id:
        kwargs["previous_interaction_id"] = args.previous_interaction_id
    if args.no_store:
        kwargs["store"] = False

    pinned = bool(args.no_fallback or args.previous_interaction_id)
    chain = [args.model]
    if not pinned:
        chain += [m for m in (args.fallback or DEFAULT_FALLBACKS) if m and m != args.model]

    keys, limits = load_config()
    try:
        inter, model = call(genai, keys, chain, kwargs, limits)
    except Fatal as e:
        die(str(e))
    text = inter.output_text or ""
    if args.json:
        print(json.dumps({"model": model, "interaction_id": inter.id, "text": text}, ensure_ascii=False))
    else:
        print(text)
        note = f" (fell back from {args.model})" if model != args.model else ""
        print(f"[ask-gemini] model={model}{note} interaction_id={inter.id}", file=sys.stderr)


if __name__ == "__main__":
    main()
