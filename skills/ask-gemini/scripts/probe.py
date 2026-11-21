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

The model is pinned (no fallback) so variants are compared on one model. Nothing is cached.
Replies go to --out DIR (one file per call plus results.jsonl); a side-by-side summary prints.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ask_gemini import DEFAULT_MODEL, Fatal, call, die, load_config  # noqa: E402

# Prefer the LTI grader's Decisions/embeddings clients: they share its per-(model, key)
# parking (persisted across processes), RPD caps, RPM pacing and 429 classification.
# Falls back to the plain rotation in post() when the LTI checkout is absent.
_LTI = Path(os.environ.get("ASK_GEMINI_LTI", Path.home() / "lti"))
try:
    sys.path[:0] = [str(_LTI / "scripts"), str(_LTI / "grader_scripts"), str(_LTI)]
    import gemini_decisions as _gd  # noqa: E402
    import gemini_embeddings as _ge  # noqa: E402
except Exception:  # noqa: BLE001 - any import failure means "no LTI", use the fallback
    _gd = _ge = None
_NO_CACHE = {"llm_cache_days": 0}  # probes always hit the API

API = "https://generativelanguage.googleapis.com/v1beta"
RETRYABLE = {429, 500, 502, 503, 504}
_NEXT = random.randrange(1 << 16)  # spread decide/embed calls across keys


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


def run_generate(args, keys, limits, variants, cases):
    from google import genai
    rows = []
    for v in variants:
        for c in cases:
            for r in range(args.runs):
                kw: dict = {"input": fill(read(v), read(c)), "store": False,
                            "generation_config": {"thinking_level": args.thinking}}
                if args.system_file:
                    kw["system_instruction"] = read(args.system_file)
                if args.schema:
                    kw["response_format"] = [{"type": "text", "mime_type": "application/json",
                                              "schema": json.loads(read(args.schema))}]
                inter, model = call(genai, keys, [args.model], kw, limits)
                rows.append({"variant": Path(v).stem, "case": Path(c).stem, "run": r + 1,
                             "model": model, "text": inter.output_text or ""})
    return rows


def run_decide(args, keys, variants, cases):
    rows = []
    for v in variants:
        questions = json.loads(read(v))
        for c in cases:
            for r in range(args.runs):
                if _gd:
                    try:
                        ans = _gd.decide(keys, read(c), questions, _NO_CACHE)
                    except _gd.DecisionError as e:
                        raise Fatal(str(e)) from e
                else:
                    ans = post(f"{API}/decisions", {"model": "gemini-decision-1", "input": read(c),
                                                    "questions": questions}, keys).get("answers")
                rows.append({"variant": Path(v).stem, "case": Path(c).stem, "run": r + 1,
                             "model": "gemini-decision-1", "text": json.dumps(ans, ensure_ascii=False)})
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
            if _ge:
                try:
                    vecs = _ge.embed(keys, texts, _NO_CACHE, model=model, task=task)
                except _ge.EmbeddingError as e:
                    raise Fatal(str(e)) from e
            else:
                body = {"requests": [embed_body(model, task, t) for t in texts]}
                vecs = [e["values"] for e in post(f"{API}/models/{model}:batchEmbedContents", body, keys)["embeddings"]]
            rows.append({"variant": spec, "case": Path(c).stem, "run": 1, "model": model,
                         "text": f"{cosine(*vecs):.3f}"})
    return rows


def summarize(rows, grep):
    pat = re.compile(grep, re.I) if grep else None
    cases = sorted({r["case"] for r in rows})
    for case in cases:
        print(f"\n## case: {case}")
        for r in (x for x in rows if x["case"] == case):
            text = r["text"].strip().replace("\n", " ")
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
    p.add_argument("-n", "--runs", type=int, help="Calls per variant x case (generate 2, decide 1).")
    p.add_argument("-m", "--model", default=DEFAULT_MODEL, help="generate: pinned model.")
    p.add_argument("--thinking", default="medium", help="generate: thinking_level (per-model enum).")
    p.add_argument("--system-file", help="generate: system instruction shared by all variants.")
    p.add_argument("--schema", help="generate: JSON schema file for structured output.")
    p.add_argument("--embed", action="append", default=[], metavar="MODEL:TASK",
                   help="embed: e.g. gemini-embedding-001:SEMANTIC_SIMILARITY, repeatable.")
    p.add_argument("--grep", help="Regex counted in every reply (e.g. names a ban should stop).")
    p.add_argument("--out", help="Directory for replies and results.jsonl.")
    args = p.parse_args()

    if args.mode != "embed" and not args.variant:
        die("give at least one --variant", 2)
    if args.mode == "embed" and not args.embed:
        die("embed mode needs at least one --embed MODEL:TASK", 2)
    args.runs = args.runs or (2 if args.mode == "generate" else 1)
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

    if args.out:
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        for r in rows:
            name = re.sub(r"[^\w.-]", "_", f"{r['variant']}__{r['case']}__{r['run']}.txt")
            (out / name).write_text(r["text"], encoding="utf-8")
        (out / "results.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                                           encoding="utf-8")
    summarize(rows, args.grep)


if __name__ == "__main__":
    main()
