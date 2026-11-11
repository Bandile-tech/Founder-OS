"""
Shared helpers for the business modules (leads, CRM, portfolio, brand).

  fuzzy_pick(rows, key_fn, fragment)  — one clear match or a short list of candidates
  money(amount, currency)             — {"amount", "currency", "usd"}
  sum_usd(rows)                       — total in dollars across currencies
  days_since(dt)                      — whole days since a date or datetime
"""

import difflib
import re
from datetime import date, datetime


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(w) > 1}


def _score(fragment: str, label: str) -> float:
    fw, lw = _words(fragment), _words(label)
    if not fw or not lw:
        return 0.0
    overlap = len(fw & lw) / min(len(fw), len(lw))
    ratio = difflib.SequenceMatcher(None, " ".join(sorted(fw)), " ".join(sorted(lw))).ratio()
    fl, ll = fragment.lower().strip(), label.lower().strip()
    contain = 1.0 if fl and (fl in ll or ll in fl) else 0.0
    return round(max(overlap * 0.9, ratio, contain), 3)


def fuzzy_pick(rows, key_fn, fragment, *, min_score=0.5, accept=0.75, gap=0.2):
    """Match ``fragment`` against ``key_fn(row)`` for every row.

    Returns ``(picked, candidates)``. ``picked`` is the row when the best score is at least
    ``accept`` and at least ``gap`` ahead of the runner-up, otherwise None. ``candidates``
    is every row scoring ``min_score`` or more as ``(score, row)``, best first. ``key_fn``
    may return one label or a list of labels (the best one counts)."""
    scored = []
    for row in rows:
        labels = key_fn(row)
        if isinstance(labels, str) or labels is None:
            labels = [labels]
        score = max((_score(fragment or "", label or "") for label in labels), default=0.0)
        if score >= min_score:
            scored.append((score, row))
    scored.sort(key=lambda pair: (-pair[0], getattr(pair[1], "id", 0) or 0))
    if not scored:
        return None, []
    top = scored[0][0]
    second = scored[1][0] if len(scored) > 1 else 0.0
    if top >= accept and (top - second) >= gap:
        return scored[0][1], scored
    return None, scored


def money(amount, currency) -> dict:
    import main
    cur = main._normalise_currency(currency)
    value = float(amount or 0)
    return {"amount": round(value, 2), "currency": cur, "usd": round(main._to_usd(value, cur), 2)}


def sum_usd(rows, amount_attr="amount", currency_attr="currency") -> float:
    import main
    total = 0.0
    for row in rows:
        amount = getattr(row, amount_attr, None)
        if amount is None:
            continue
        total += main._to_usd(float(amount), getattr(row, currency_attr, None))
    return round(total, 2)


def days_since(dt) -> int | None:
    """Whole days from ``dt`` (a date or a naive UTC datetime) to now. None for no date."""
    if dt is None:
        return None
    if isinstance(dt, datetime):
        return (datetime.utcnow() - dt).days
    if isinstance(dt, date):
        return (date.today() - dt).days
    return None
