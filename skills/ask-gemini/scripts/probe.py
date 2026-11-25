#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["google-genai>=2.3.0"]
# ///
"""Quick prompt probes: compare 2-4 wordings on a few cases before any bench.

Modes:
  generate  each variant file is a prompt; {{case}} is replaced by the case text (else the case
            is appended in <case> tags). Runs = calls per variant x case. Optional --schema.
  decide    each variant file is a Decisions `questions` JSON object; each case is the input.
            POST /v1beta/decisions (gemini-decision-1). Deterministic, so runs default to 1.
  embed     each case file holds two texts split by a line `---`; prints cosine per
            (model, task) given by --embed MODEL:TASK (repeatable).

The model is pinned (no fallback) so variants are compared on one model. With --fallback-class,
generate falls back within the pinned model's class (flash-lite, flash, pro), newer versions
first; all variants and runs of one case share one model, and a case that fails partway is rerun
whole on the next model. Class members come from config "model_classes" {class: [ids]} if set,
else from every gemini-* id in the config plus ASK_GEMINI_MODEL/FALLBACKS. Nothing is cached.
Replies go to --out DIR (one file per call plus results.jsonl); a side-by-side summary prints.
"""
from __future__ import annotations

import argparse
import json
import math
import random
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ask_gemini import DEFAULT_FALLBACKS, DEFAULT_MODEL, Fatal, _load_keys, call, die, load_config  # noqa: E402

import quota_hook  # noqa: E402

API = "https://generativelanguage.googleapis.com/v1beta"
RETRYABLE = {429, 500, 502, 503, 504}
_NEXT = random.randrange(1 << 16)  # spread decide/embed calls across keys
OUT: Path | None = None
ID_RE = re.compile(r"(?<!\d)\d{9}(?!\d)")  # HUFS student id


def version() -> str:
    try:
        return json.loads((Path(__file__).resolve().parents[3] / ".claude-plugin" / "plugin.json").read_text())["version"]
    except (OSError, KeyError, ValueError):
        return "unknown"


def emit(rows: list[dict]) -> list[dict]:
    """Append rows to results.jsonl and per-call files now, so a timeout keeps finished cells."""
    if OUT:
        with (OUT / "results.jsonl").open("a", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
                name = re.sub(r"[^\w.-]", "_", f"{r['variant']}__{r['case']}__{r['run']}.txt")
                (OUT / name).write_text(r.get("text") or f"ERROR: {r.get('error')}", encoding="utf-8")
    return rows


def failed(variant: str, case: str, run: int, model: str, err: Exception) -> dict:
    print(f"FAILED {variant} x {case} run{run}: {str(err)[:200]}", file=sys.stderr)
    return {"variant": variant, "case": case, "run": run, "model": model, "text": "", "error": str(err)[:500]}


def strip_private(obj):
    """Drop `_`-prefixed keys (notes, sets) the Decisions API rejects."""
    if isinstance(obj, dict):
        return {k: strip_private(v) for k, v in obj.items() if not str(k).startswith("_")}
    if isinstance(obj, list):
        return [strip_private(v) for v in obj]
    return obj


def read(p: str) -> str:
    return Path(p).read_text(encoding="utf-8")


def fill(prompt: str, case: str) -> str:
    return prompt.replace("{{case}}", case) if "{{case}}" in prompt else f"{prompt}\n\n<case>\n{case}\n</case>"


def post(url: str, body: dict, keys: list[str]) -> dict:
    """Rotate: start at the next key each call, try each key once; retry only on quota/server
    errors. Never prints keys."""
    global _NEXT
    errors = []
    start, _NEXT = _NEXT, (_NEXT + 1) % len(keys)
    for i in [(start + j) % len(keys) for j in range(len(keys))]:
        key = keys[i]
        req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "x-goog-api-key": key})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            msg = e.read().decode("utf-8", "replace")[:300]
            if e.code not in RETRYABLE:
                raise Fatal(f"HTTP {e.code}: {msg}") from e
            errors.append(f"key#{i + 1}: HTTP {e.code}")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            errors.append(f"key#{i + 1}: {type(e).__name__}")
    raise Fatal(f"no key answered ({'; '.join(errors)})")


def model_class(model: str) -> str | None:
    m = model.lower()
    if "flash-lite" in m:
        return "flash-lite"
    if "-flash" in m or "hydrogen" in m:  # Hydrogen is the Flash successor
        return "flash"
    if "-pro" in m:
        return "pro"
    return None


def version_key(model: str) -> tuple:
    nums = re.findall(r"\d+(?:\.\d+)?", model)
    v = float(nums[0]) if nums else 0.0
    return (-v, "preview" in model, model)  # newest first, GA before preview


def class_chain(pinned: str) -> list[str]:
    cls = model_class(pinned)
    if not cls:
        die(f"--fallback-class: no known class for {pinned} (flash-lite, flash, pro)", 2)
    _, _, data = _load_keys()
    data = data if isinstance(data, dict) else {}
    named = data.get("model_classes", {}).get(cls) if isinstance(data.get("model_classes"), dict) else None
    if named:
        pool = [m for m in named if isinstance(m, str)]
    else:
        pool = set(re.findall(r'"(gemini-[\w.-]+)"', json.dumps(data))) | {DEFAULT_MODEL, *DEFAULT_FALLBACKS}
        pool = [m for m in pool if model_class(m) == cls and "embedding" not in m and "decision" not in m]
    return [pinned] + sorted({m for m in pool if m != pinned}, key=version_key)


def run_generate(args, keys, limits, variants, cases):
    try:
        from google import genai
    except ImportError:
        die("google-genai not importable; run with: uv run --script " + str(Path(__file__).resolve()), 2)
    chain = class_chain(args.model) if args.fallback_class else [args.model]
    if args.fallback_class:
        print(f"class chain: {', '.join(chain)}", file=sys.stderr)
    rows = []
    for c in cases:
        errors = []
        for model in chain:
            try:
                rows += emit(generate_case(genai, args, keys, limits, variants, c, model))
                break
            except Fatal as e:
                errors.append(f"{model}: {str(e)[:200]}")
                if model != chain[-1]:
                    print(f"case {Path(c).stem}: {model} failed, rerunning case on next model", file=sys.stderr)
        else:
            err = Fatal("; ".join(errors))
            rows += emit([failed(Path(v).stem, Path(c).stem, r + 1, chain[-1], err)
                          for v in variants for r in range(args.runs)])
    return rows


def generate_case(genai, args, keys, limits, variants, c, model):
    rows = []
    for v in variants:
        for r in range(args.runs):
            kw: dict = {"input": fill(read(v), read(c)), "store": False,
                        "generation_config": {"thinking_level": args.thinking}}
            if args.system_file:
                kw["system_instruction"] = read(args.system_file)
            if args.schema:
                kw["response_format"] = [{"type": "text", "mime_type": "application/json",
                                          "schema": json.loads(read(args.schema))}]
            inter, used = call(genai, keys, [model], kw, limits)
            rows.append({"variant": Path(v).stem, "case": Path(c).stem, "run": r + 1,
                         "model": used, "text": inter.output_text or ""})
    return rows


def run_decide(args, keys, variants, cases):
    rows = []
    for v in variants:
        questions = strip_private(json.loads(read(v)))
        for c in cases:
            for r in range(args.runs):
                hook = quota_hook.get("decide")
                try:
                    if hook:
                        try:
                            ans = hook(keys, read(c), questions, {**quota_hook.config(), "llm_cache_days": 0})
                        except Exception as e:  # noqa: BLE001 - hook errors become a failed cell
                            raise Fatal(f"quota hook decide: {e}") from e
                    else:
                        ans = post(f"{API}/decisions", {"model": "gemini-decision-1", "input": read(c),
                                                        "questions": questions}, keys).get("answers")
                except Fatal as e:
                    if "HTTP 400" in str(e):
                        raise  # a bad question fails every cell the same way
                    rows += emit([failed(Path(v).stem, Path(c).stem, r + 1, "gemini-decision-1", e)])
                    continue
                rows += emit([{"variant": Path(v).stem, "case": Path(c).stem, "run": r + 1,
                               "model": "gemini-decision-1", "text": json.dumps(ans, ensure_ascii=False)}])
    return rows


def embed_body(model: str, task: str, text: str) -> dict:
    r: dict = {"model": f"models/{model}"}
    if model.endswith("001"):
        r.update(content={"parts": [{"text": text}]}, taskType=task)
    else:  # newer models take the task as a text prefix
        r["content"] = {"parts": [{"text": f"task: {task.lower().replace('_', ' ')} | query: {text}"}]}
    return r


def cosine(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    return dot / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)) or 1.0)


def run_embed(args, keys, cases):
    rows = []
    for spec in args.embed:
        model, _, task = spec.partition(":")
        task = task or "SEMANTIC_SIMILARITY"
        for c in cases:
            parts = re.split(r"^---\s*$", read(c), flags=re.M)
            if len(parts) != 2:
                die(f"{c}: embed cases need exactly two texts split by a line '---'")
            texts = [p.strip() for p in parts]
            hook = quota_hook.get("embed")
            try:
                if hook:
                    try:
                        vecs = hook(keys, texts, {**quota_hook.config(), "llm_cache_days": 0}, model=model, task=task)
                    except Exception as e:  # noqa: BLE001
                        raise Fatal(f"quota hook embed: {e}") from e
                else:
                    body = {"requests": [embed_body(model, task, t) for t in texts]}
                    vecs = [e["values"] for e in post(f"{API}/models/{model}:batchEmbedContents", body, keys)["embeddings"]]
            except Fatal as e:
                if "HTTP 400" in str(e):
                    raise
                rows += emit([failed(spec, Path(c).stem, 1, model, e)])
                continue
            rows += emit([{"variant": spec, "case": Path(c).stem, "run": 1, "model": model,
                           "text": f"{cosine(*vecs):.3f}"}])
    return rows


def summarize(rows, grep):
    pat = re.compile(grep, re.I) if grep else None
    cases = sorted({r["case"] for r in rows})
    pinned = rows[0]["model"] if rows else None
    for case in cases:
        models = sorted({r["model"] for r in rows if r["case"] == case})
        note = f" (ran on {', '.join(models)})" if models != [pinned] else ""
        print(f"\n## case: {case}{note}")
        for r in (x for x in rows if x["case"] == case):
            text = f"FAILED: {r['error'][:200]}" if r.get("error") else r["text"].strip().replace("\n", " ")
            hit = f" [grep:{len(pat.findall(r['text']))}]" if pat else ""
            print(f"- {r['variant']} run{r['run']}{hit}: {text[:400]}")
    if pat:
        print("\n## grep matches per variant")
        for v in sorted({r["variant"] for r in rows}):
            n = sum(len(pat.findall(r["text"])) for r in rows if r["variant"] == v)
            calls = sum(1 for r in rows if r["variant"] == v)
            print(f"- {v}: {n} in {calls} calls")


def main() -> None:
    p = argparse.ArgumentParser(description="Compare prompt wordings on a few cases.")
    p.add_argument("mode", choices=["generate", "decide", "embed"])
    p.add_argument("-v", "--variant", action="append", default=[], help="Variant file, repeatable.")
    p.add_argument("-c", "--case", action="append", required=True, help="Case file, repeatable.")
    p.add_argument("-n", "--runs", type=int, help="Calls per variant x case (generate 2; decide/embed 1, deterministic).")
    p.add_argument("-m", "--model", default=DEFAULT_MODEL, help="generate: pinned model.")
    p.add_argument("--fallback-class", action="store_true",
                   help="generate: on failure rerun the case on another model of the same class.")
    p.add_argument("--thinking", default="medium", help="generate: thinking_level (per-model enum).")
    p.add_argument("--system-file", help="generate: system instruction shared by all variants.")
    p.add_argument("--schema", help="generate: JSON schema file for structured output.")
    p.add_argument("--embed", action="append", default=[], metavar="MODEL:TASK",
                   help="embed: e.g. gemini-embedding-001:SEMANTIC_SIMILARITY, repeatable.")
    p.add_argument("--grep", help="Regex counted in every reply (e.g. names a ban should stop).")
    p.add_argument("--out", help="Directory for replies and results.jsonl (written after each call).")
    p.add_argument("--allow-ids", action="store_true", help="Allow 9-digit student ids in case files.")
    args = p.parse_args()

    if args.mode != "embed" and not args.variant:
        die("give at least one --variant", 2)
    if args.mode == "embed" and not args.embed:
        die("embed mode needs at least one --embed MODEL:TASK", 2)
    args.runs = args.runs or (2 if args.mode == "generate" else 1)  # decide is deterministic
    if not args.allow_ids:
        bad = [c for c in args.case if ID_RE.search(read(c))]
        if bad:
            die(f"9-digit student id in {', '.join(bad)}: redact first (or --allow-ids)", 2)
    print(f"probe.py {version()}", file=sys.stderr)
    global OUT
    if args.out:
        OUT = Path(args.out)
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / "results.jsonl").write_text("", encoding="utf-8")
    keys, limits = load_config()
    try:
        if args.mode == "generate":
            rows = run_generate(args, keys, limits, args.variant, args.case)
        elif args.mode == "decide":
            rows = run_decide(args, keys, args.variant, args.case)
        else:
            rows = run_embed(args, keys, args.case)
    except Fatal as e:
        die(str(e))

    summarize(rows, args.grep)
    bad = [r for r in rows if r.get("error")]
    print(f"\n{len(rows) - len(bad)}/{len(rows)} calls ok", file=sys.stderr)
    if bad:
        sys.exit(1)


if __name__ == "__main__":
    main()
