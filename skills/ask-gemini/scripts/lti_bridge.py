"""Optional bridge to the TABot LTI grader's quota state, for every Gemini model.

When ~/lti (or $ASK_GEMINI_LTI) holds the grader and ask-gemini uses the same key list in
the same order, each call goes through the grader's own gates: its persisted per-(model, key)
parks (~/.tabot/gemini-parks.json), its RPD caps (config llm_rpd_limits), its cross-process
RPM pacing (~/.tabot/throttle), and its 429 classifier, which writes parks back so the grader
sees what ask-gemini spent. Without the LTI, or with other keys, every hook is a no-op.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

LTI = Path(os.environ.get("ASK_GEMINI_LTI", Path.home() / "lti"))
_g = None  # the grader module, once enabled
_cfg: dict = {}


def enable(keys: list[str], config_path: Path) -> bool:
    """Turn the bridge on when the grader imports and its key list matches ours."""
    global _g, _cfg
    if _g is not None:
        return True
    try:
        cfg = json.loads(Path(config_path).read_text(encoding="utf-8"))
        sys.path[:0] = [str(LTI / "scripts"), str(LTI / "grader_scripts"), str(LTI)]
        import grader  # noqa: PLC0415
    except Exception:  # noqa: BLE001 - no LTI or unreadable config: stay off
        return False
    if not isinstance(cfg, dict) or grader._resolve_keys(cfg.get("llm_api_key")) != keys:
        return False  # different keys or order: indexes would not line up
    _g, _cfg = grader, cfg
    return True


def on() -> bool:
    return _g is not None


def config() -> dict:
    return _cfg


def parked(model: str, idx: int) -> bool:
    """True when the grader holds (model, key) parked or at its daily cap."""
    return _g is not None and _g._quota_gate(model, idx, _cfg) is not None


def before_call(model: str, idx: int, key: str) -> None:
    """Pace on the shared per-(model, key) RPM gate and count the request."""
    if _g is None:
        return
    _g._throttle_for_model(model, _g._min_interval_for_model(model, _cfg), idx, key[-8:])
    _g._rpd_note_request(model, idx)


def note_429(model: str, idx: int, text: str) -> None:
    """Record a quota 429 in the grader's park file (classified by the grader)."""
    if _g is not None:
        _g._note_quota_429(model, idx, text)
