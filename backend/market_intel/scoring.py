"""
Opportunity scoring — a weighted mean of the seven axes and the recommendation band.

Weights live in ``context/core/opportunity_weights.json`` so they can be tuned without a
deploy. A missing or invalid file falls back to ``DEFAULT_WEIGHTS``.
"""

import json
from pathlib import Path

WEIGHTS_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "context" / "core" / "opportunity_weights.json"
)

DEFAULT_WEIGHTS = {
    "pain_severity": 1.5, "frequency": 1.0, "financial_value": 1.5,
    "market_size": 0.75, "competition_gap": 1.0, "founder_fit": 2.0, "speed_to_mvp": 1.25,
}


def weights() -> dict:
    """The weights file when it is a complete, positive set of numbers; defaults otherwise."""
    try:
        raw = json.loads(WEIGHTS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return dict(DEFAULT_WEIGHTS)
    if not isinstance(raw, dict):
        return dict(DEFAULT_WEIGHTS)
    out = {}
    for axis in DEFAULT_WEIGHTS:
        value = raw.get(axis)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            return dict(DEFAULT_WEIGHTS)
        out[axis] = float(value)
    if sum(out.values()) <= 0:
        return dict(DEFAULT_WEIGHTS)
    return out


def _axis(scores: dict, axis: str) -> float:
    try:
        return min(10.0, max(0.0, float((scores or {}).get(axis))))
    except (TypeError, ValueError):
        return 0.0


def overall(scores: dict) -> float:
    """Weighted mean of the seven axes, one decimal place."""
    w = weights()
    total = sum(w.values())
    return round(sum(_axis(scores, axis) * weight for axis, weight in w.items()) / total, 1)


def recommendation(overall_score: float) -> str:
    """pursue at 7.5 and above, watch at 6.0 and above, park below."""
    try:
        value = float(overall_score)
    except (TypeError, ValueError):
        return "park"
    if value >= 7.5:
        return "pursue"
    if value >= 6.0:
        return "watch"
    return "park"
