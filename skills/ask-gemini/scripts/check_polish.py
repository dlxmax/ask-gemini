#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["markdown-it-py>=3"]
# ///
"""Check a polished Markdown/text file against its original.

Usage: check_polish.py [--fix] [--korean] ORIG NEW [ORIG NEW ...]

Reports, per pair:
  - line count change
  - changed lines that had no target-language text (default: any non-ASCII letter;
    --korean: Hangul), changed headings, lost trailing two-space breaks
  - link targets, inline code, URLs, numbers and <br> that differ (as multisets)
  - literal * or ** in the rendered output that the original didn't have
    (broken bold/italic, e.g. a particle glued after a closing **)
  - with --korean: leftover "Latin/digit + space + particle" spacing (`v1 은`)
  - a wording-only diff: changed lines, skipping those whose only change is spacing

--fix rewrites NEW in place first: strips a wrapping ``` fence, restores the
original's line endings (CRLF/LF) and final-newline state.
Exit status 1 if any pair has problems.
"""
import re
import sys

HANGUL = re.compile(r"[가-힣]")
NONASCII_LETTER = re.compile(r"[^\W\d_a-zA-Z]")
PARTICLE = re.compile(
    r"(?<=[A-Za-z0-9)\]`'\"*])[ ]"
    r"(은|는|이|가|을|를|과|와|의|도|만|에|에서|에게|으로|로|으로는|로는|이나|나|까지|부터|보다|처럼|이에요|예요|입니다)"
    r"(?=[\s.,;:!?)\]]|$)"
)
CHECKS = [
    (r"\]\(([^)\s]*)", "link targets"),
    (r"`[^`\n]*`", "inline code"),
    (r"https?://[^\s)`>\]]+", "URLs"),
    (r"\d+", "numbers"),
    (r"<br\s*/?>", "<br>"),
]


def read(path):
    with open(path, "rb") as f:
        return f.read().decode("utf-8")


def fix(orig, new_path):
    new = read(new_path)
    m = re.fullmatch(r"\s*```[\w-]*\n(.*?)\n?```\s*", new, re.S)
    if m:
        new = m.group(1)
    new = new.replace("\r\n", "\n")
    if "\r\n" in orig:
        new = new.replace("\n", "\r\n")
    nl = "\r\n" if "\r\n" in orig else "\n"
    new = new.rstrip("\r\n")
    if orig.endswith(("\n", "\r")):
        new += nl
    with open(new_path, "wb") as f:
        f.write(new.encode("utf-8"))
    return new


def literal_stars(text):
    from markdown_it import MarkdownIt
    html = MarkdownIt("commonmark").enable("table").render(text)
    html = re.sub(r"<(code|pre)>.*?</\1>", "", html, flags=re.S)
    out = []
    for line in html.splitlines():
        t = re.sub(r"<[^>]+>", "", line)
        if "*" in t:
            out.append(t.strip())
    return out


def squash(s):
    return re.sub(r"\s+", "", s)


def check(orig_path, new_path, korean, do_fix):
    a = read(orig_path)
    b = fix(a, new_path) if do_fix else read(new_path)
    target = HANGUL if korean else NONASCII_LETTER
    A, B = a.splitlines(), b.splitlines()
    probs = []

    if len(A) != len(B):
        probs.append(f"line count {len(A)} -> {len(B)} (per-line checks below may be misaligned)")
    if ("\r\n" in a) != ("\r\n" in b):
        probs.append("line endings changed (use --fix)")
    if a.endswith("\n") != b.endswith("\n"):
        probs.append("final newline changed (use --fix)")

    for rx, name in CHECKS:
        xa, xb = sorted(re.findall(rx, a)), sorted(re.findall(rx, b))
        if xa != xb:
            lost = [x for x in set(xa) if xa.count(x) > xb.count(x)][:5]
            gained = [x for x in set(xb) if xb.count(x) > xa.count(x)][:5]
            probs.append(f"{name} differ: lost {lost} gained {gained}")

    wording = []
    for i, (x, y) in enumerate(zip(A, B), 1):
        if x == y:
            continue
        if x.lstrip().startswith("#"):
            probs.append(f"L{i}: heading changed")
        elif not target.search(x):
            probs.append(f"L{i}: line without target-language text changed")
        if x.endswith("  ") and not y.endswith("  "):
            probs.append(f"L{i}: trailing two-space break lost")
        if squash(x) != squash(y):
            wording.append((i, x, y))

    old_stars = {squash(s) for s in literal_stars(a)}
    for s in literal_stars(b):
        if squash(s) not in old_stars:
            probs.append(f"literal * in rendered output (broken emphasis?): {s[:120]}")

    if korean:
        for i, y in enumerate(B, 1):
            for m in PARTICLE.finditer(y):
                probs.append(f"L{i}: spaced particle '{y[max(0, m.start() - 12):m.end()]}'")

    changed = sum(x != y for x, y in zip(A, B))
    status = "OK" if not probs else f"{len(probs)} PROBLEM(S)"
    print(f"== {new_path}: {changed} lines changed, {len(wording)} beyond spacing; {status}")
    for p in probs[:40]:
        print("  !", p)
    if len(probs) > 40:
        print(f"  ! ... {len(probs) - 40} more")
    for i, x, y in wording:
        print(f"  L{i}\n    - {x.strip()}\n    + {y.strip()}")
    return not probs


def main():
    args = sys.argv[1:]
    do_fix = "--fix" in args
    korean = "--korean" in args
    paths = [p for p in args if not p.startswith("--")]
    if not paths or len(paths) % 2:
        sys.exit(__doc__)
    ok = all([check(o, n, korean, do_fix) for o, n in zip(paths[::2], paths[1::2])])
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
