"""Optional quota hook: lets ask-gemini share quota state with another tool on the same keys.

If $ASK_GEMINI_QUOTA_HOOK (default ~/.config/ask-gemini/quota_hook.py) exists, it is loaded
once the key list is known. It may define any of:

  enable(keys, config: dict) -> bool   turn on (e.g. only if the other tool uses the same keys)
  parked(model, key_idx) -> bool        skip this (model, key): parked or at a daily cap
  before_call(model, key_idx, key)      pace and count before each request
  note_429(model, key_idx, error_text)  record a quota error
  config() -> dict                      extra config passed to decide/embed
  decide(keys, input, questions, config) / embed(keys, texts, config, model=, task=)
                                        replace probe.py's own Decisions/embeddings calls

Missing functions are no-ops. Without a hook file, ask-gemini uses only its own parks.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

PATH = Path(os.environ.get("ASK_GEMINI_QUOTA_HOOK", Path.home() / ".config" / "ask-gemini" / "quota_hook.py"))
_h = None


def enable(keys: list[str], config: dict) -> bool:
    global _h
    if _h is not None or not PATH.is_file():
        return _h is not None
    try:
        spec = importlib.util.spec_from_file_location("ask_gemini_user_quota_hook", PATH.resolve())
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        ok = getattr(mod, "enable", lambda *_: True)(keys, config)
    except Exception as e:  # noqa: BLE001 - a broken hook must not break calls
        print(f"ask-gemini: quota hook {PATH} not loaded ({type(e).__name__})", file=sys.stderr)
        return False
    if ok:
        _h = mod
    return bool(ok)


def on() -> bool:
    return _h is not None


def get(name: str):
    return getattr(_h, name, None) if _h else None


def parked(model: str, idx: int) -> bool:
    f = get("parked")
    return bool(f and f(model, idx))


def before_call(model: str, idx: int, key: str) -> None:
    f = get("before_call")
    if f:
        f(model, idx, key)


def note_429(model: str, idx: int, text: str) -> None:
    f = get("note_429")
    if f:
        f(model, idx, text)


def config() -> dict:
    f = get("config")
    return dict(f()) if f else {}
