"""
Personal finance ledger (Phase 7, Stage 2).

Bandile says what he spent or received and every balance is up to date.

The rules of the money path:
  * no floats. An amount is whole minor units (ngwee for ZMW, cents for USD), parsed from
    what he said with ``Decimal``. A percentage is whole basis points. The model never
    does arithmetic: it passes the words, and everything below is integer maths.
  * no stored balance. A balance is the sum of a bucket's transactions, every time.
  * a split that leaves ngwee over gives them to the bucket marked as the remainder
    bucket, so the legs of an income always add up to exactly what was received.
  * the same money is recorded once. A client payment that becomes paid in the CRM writes
    its income here in the same batch (``on_payment_paid``, linked by ``payment_id``);
    revenue logged through a brain dump does the same (``on_revenue_logged``). When the
    same turn has already recorded that money, the existing entry is linked, not copied.

Every write goes through the change log, so a batch (one message) is undone as one.

  log_transactions(db, items)          the tool: a list, one batch
  set_allocation_rules(db, rules, …)   the tool: the split for an income type
  get_finance(db, view, period, …)     the tool: balances, spending, summary, transactions
  GET   /finance/summary               buckets, money in and out, spending by category
  GET   /finance/transactions          the list
  PATCH /finance/transactions/{id}     one-tap edit (bucket, category, note, amount, day)
  DELETE /finance/transactions/{id}    remove an entry (the screen asks first)
  POST  /finance/buckets/{id}/default  spend from this bucket when none is named
"""

import re
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import case, func
from sqlalchemy.orm import Session

import change_log
import models
import user_clock
from database import get_db

router = APIRouter()

FULL = 10000                      # basis points in 100%
TRANSFER = "transfer"             # category of a move between buckets: not income, not spending
UNALLOCATED = "Unallocated"
DEFAULT_TYPE = "default"
BUSINESS_TYPES = {"business", "client", "sale", "sales", "revenue", "freelance", "work"}
_SYMBOL = {"ZMW": "K", "USD": "$"}
_OUT_WORDS = {"out", "spent", "spend", "spending", "expense", "paid", "bought", "gave"}
_IN_WORDS = {"in", "received", "receive", "got", "income", "earned", "paid in"}
_CENT = Decimal("0.01")
_MAX_MINOR = 10 ** 13             # K100 billion: anything above is a typing slip

_MONEY = re.compile(
    r"^\s*(?P<pre>zmw|zk|usd|us\$|k|\$)?\s*(?P<num>[0-9][0-9,]*(?:\.[0-9]+)?|\.[0-9]+)\s*"
    r"(?P<post>zmw|kwacha|usd|dollars?|\$|k)?\s*$", re.I)
_CURRENCY_WORD = {"k": "ZMW", "zk": "ZMW", "zmw": "ZMW", "kwacha": "ZMW",
                  "$": "USD", "us$": "USD", "usd": "USD", "dollar": "USD", "dollars": "USD"}


# ── Money: words in, whole minor units out ───────────────────

def to_minor(value) -> int:
    """A Decimal (or the text of one) as whole minor units. More than two decimal places
    is refused rather than rounded: the ledger does not invent ngwee."""
    try:
        amount = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(f"'{value}' is not an amount")
    if not amount.is_finite():
        raise ValueError(f"'{value}' is not an amount")
    if amount != amount.quantize(_CENT):
        raise ValueError(f"'{value}' has more than two decimal places")
    minor = int(amount * 100)
    if minor <= 0:
        raise ValueError("an amount must be above zero")
    if minor > _MAX_MINOR:
        raise ValueError(f"'{value}' is too large to be right")
    return minor


def parse_money(value, default_currency: str | None = None) -> tuple[int, str]:
    """``(minor units, currency)`` from what he said: "K50", "1,250.50", "$20", "800 kwacha".
    A bare number is in ``default_currency`` (else the app's default)."""
    default = (default_currency or models.default_currency()).upper()
    if isinstance(value, bool) or value is None:
        raise ValueError("an amount is missing")
    if isinstance(value, int):
        return to_minor(Decimal(value)), default
    if isinstance(value, float):
        # A JSON number: its shortest repr is the figure that was written.
        return to_minor(Decimal(repr(value))), default
    text = str(value).strip()
    found = _MONEY.match(text)
    if not found:
        raise ValueError(f"could not read the amount '{text}'")
    pre, post = (found.group("pre") or "").lower(), (found.group("post") or "").lower()
    if post == "k":
        raise ValueError(f"'{text}' could mean thousands or Kwacha: write the full figure, like K5,000")
    currency = _CURRENCY_WORD.get(pre) or _CURRENCY_WORD.get(post) or default
    return to_minor(Decimal(found.group("num").replace(",", ""))), currency


def float_to_minor(amount) -> int:
    """A CRM amount (the CRM stores floats) as minor units, rounded to the ngwee once, here."""
    return int((Decimal(str(amount)).quantize(_CENT, rounding=ROUND_HALF_UP)) * 100)


def fmt(minor: int, currency: str = "ZMW") -> str:
    """K1,250 · K1,250.50 · -K20 · $50."""
    minor = int(minor)
    whole, part = divmod(abs(minor), 100)
    text = f"{whole:,}" + (f".{part:02d}" if part else "")
    symbol = _SYMBOL.get((currency or "ZMW").upper())
    body = f"{symbol}{text}" if symbol else f"{currency} {text}"
    return f"-{body}" if minor < 0 else body


def parse_pct(value) -> int:
    """A percentage as basis points: 10 → 1000, "12.5%" → 1250."""
    try:
        pct = Decimal(str(value).strip().rstrip("%").strip())
    except (InvalidOperation, ValueError):
        raise ValueError(f"'{value}' is not a percentage")
    points = pct * 100
    if points != points.to_integral_value() or points <= 0 or points > FULL:
        raise ValueError(f"'{value}' is not a usable percentage (up to two decimal places, above 0)")
    return int(points)


def pct_text(points: int) -> str:
    whole, part = divmod(int(points), 100)
    return f"{whole}%" if not part else f"{whole}.{part:02d}".rstrip("0") + "%"


# ── Buckets ──────────────────────────────────────────────────

def bucket_key(name) -> str:
    return " ".join(str(name or "").lower().split())


def _stem(name) -> str:
    """"savings", "saving" and "save" are one bucket; so are "invest" and "investments"."""
    word = re.sub(r"[^a-z0-9]", "", bucket_key(name))
    for ending in ("ments", "ment", "ings", "ing", "es", "s", "e"):
        if word.endswith(ending) and len(word) - len(ending) >= 3:
            return word[: -len(ending)]
    return word


def find_bucket(db: Session, name) -> models.Bucket | None:
    key = bucket_key(name)
    if not key:
        return None
    rows = db.query(models.Bucket).all()
    for row in rows:
        if row.key == key:
            return row
    close = [row for row in rows if _stem(row.key) == _stem(key)]
    return close[0] if len(close) == 1 else None


def ensure_bucket(db: Session, name, **flags) -> models.Bucket:
    found = find_bucket(db, name)
    if found is not None:
        return found
    label = " ".join(str(name).split())
    label = label if any(ch.isupper() for ch in label) else label.capitalize()
    row = models.Bucket(name=label, key=bucket_key(name), created_at=datetime.utcnow(), updated_at=datetime.utcnow(),
                        is_default=bool(flags.get("is_default")), is_remainder=bool(flags.get("is_remainder")),
                        is_unallocated=bool(flags.get("is_unallocated")))
    db.add(row)
    change_log.record_create(db, row, f"New bucket: {row.name}")
    return row


def unallocated_bucket(db: Session, create: bool = True) -> models.Bucket | None:
    row = db.query(models.Bucket).filter(models.Bucket.is_unallocated.is_(True)).first()
    if row is None and create:
        row = ensure_bucket(db, UNALLOCATED, is_unallocated=True)
        if not row.is_unallocated:               # a bucket he happened to name "Unallocated"
            before = change_log.snapshot(row)
            row.is_unallocated = True
            change_log.record_update(db, row, before, f"{row.name} holds income that has no split yet")
    return row


def user_buckets(db: Session) -> list[models.Bucket]:
    return [b for b in db.query(models.Bucket).order_by(models.Bucket.id).all() if not b.is_unallocated]


def default_bucket(db: Session) -> models.Bucket | None:
    return db.query(models.Bucket).filter(models.Bucket.is_default.is_(True)).first()


def _mark_only(db: Session, bucket: models.Bucket, flag: str, summary: str) -> None:
    """Set ``flag`` on this bucket and clear it on every other one."""
    for other in db.query(models.Bucket).all():
        wanted = other.id == bucket.id
        if bool(getattr(other, flag)) != wanted:
            before = change_log.snapshot(other)
            setattr(other, flag, wanted)
            other.updated_at = datetime.utcnow()
            change_log.record_update(db, other, before, summary if wanted else f"{other.name}: no longer that")


def _spend_bucket(db: Session, named) -> tuple[models.Bucket | None, str | None]:
    """The bucket a purchase comes out of, or the one short question to ask instead."""
    mine = user_buckets(db)
    names = ", ".join(b.name for b in mine)
    if named:
        found = find_bucket(db, named)
        if found is None:
            return None, (f"There is no '{named}' bucket. Which one did it come out of: {names}?" if mine
                          else f"There is no '{named}' bucket yet. Set your buckets first, or say it again without one.")
        return found, None
    chosen = default_bucket(db)
    if chosen is not None:
        return chosen, None
    if not mine:
        return unallocated_bucket(db), None
    if len(mine) == 1:
        return mine[0], None
    return None, f"Which bucket did it come out of: {names}?"


# ── Rules and the split ──────────────────────────────────────

def income_type_key(value) -> str:
    return bucket_key(value) or DEFAULT_TYPE


def rules_for(db: Session, income_type) -> list[models.AllocationRule]:
    """The rules for this income type, else the default rules, else none."""
    for key in dict.fromkeys([income_type_key(income_type), DEFAULT_TYPE]):
        rows = db.query(models.AllocationRule).filter(
            models.AllocationRule.income_type == key).order_by(models.AllocationRule.id).all()
        if rows:
            return rows
    return []


def remainder_bucket_id(db: Session) -> int | None:
    row = db.query(models.Bucket).filter(models.Bucket.is_remainder.is_(True)).first()
    return row.id if row else None


def split(amount_minor: int, shares: list[tuple[int, int]], remainder_bucket: int | None = None) -> list[tuple[int, int]]:
    """``[(bucket id, minor units)]`` for an income. Each share is rounded down to the
    ngwee; what is left over goes to the remainder bucket (the largest share when none is
    marked). The parts always add up to ``amount_minor``."""
    parts = [[bucket, amount_minor * points // FULL] for bucket, points in shares]
    left = amount_minor - sum(part for _, part in parts)
    if left:
        target = remainder_bucket if remainder_bucket is not None else max(shares, key=lambda s: s[1])[0]
        for part in parts:
            if part[0] == target:
                part[1] += left
                break
        else:
            parts.append([target, left])
    return [(bucket, part) for bucket, part in parts if part > 0]


# ── Writing ──────────────────────────────────────────────────

def _names(db: Session) -> dict:
    return {b.id: b.name for b in db.query(models.Bucket).all()}


def _row(db, *, entry_id, day, direction, minor, currency, bucket_id, category, note, income_type=None,
         payment_id=None, revenue_id=None, summary: str) -> models.LedgerTransaction:
    row = models.LedgerTransaction(
        entry_id=entry_id, created_at=datetime.utcnow(), occurred_at=day, direction=direction,
        amount_minor=int(minor), currency=currency, bucket_id=bucket_id,
        category=(bucket_key(category) or None), note=(str(note).strip()[:300] or None) if note else None,
        income_type=income_type, payment_id=payment_id, revenue_id=revenue_id,
        batch_id=change_log.current_batch_id(db))
    db.add(row)
    change_log.record_create(db, row, summary)
    return row


def add_income(db: Session, *, amount_minor: int, currency: str, day: date | None = None, income_type=None,
               source: str | None = None, note: str | None = None, category: str | None = None,
               payment_id: int | None = None, revenue_id: int | None = None) -> dict:
    """Record money received, split across the buckets by the rules for its income type
    (into Unallocated when there are none). The caller commits."""
    day = day or user_clock.today()
    kind = income_type_key(income_type)
    with change_log.batch_scope(db, "assistant"):
        rules = rules_for(db, kind)
        if rules:
            parts = split(amount_minor, [(r.bucket_id, r.pct) for r in rules], remainder_bucket_id(db))
        else:
            parts = [(unallocated_bucket(db).id, amount_minor)]
        names = _names(db)
        entry = uuid.uuid4().hex
        whole = fmt(amount_minor, currency) + (f" from {source}" if source else "")
        rows = []
        for bucket_id, minor in parts:
            summary = (f"Received {whole} into {names[bucket_id]}" if len(parts) == 1
                       else f"{fmt(minor, currency)} of {whole} to {names[bucket_id]}")
            rows.append(_row(db, entry_id=entry, day=day, direction="in", minor=minor, currency=currency,
                             bucket_id=bucket_id, category=category or "income", note=note or source,
                             income_type=kind, payment_id=payment_id, revenue_id=revenue_id, summary=summary))
    return {"entry_id": entry, "amount_minor": amount_minor, "currency": currency, "unallocated": not rules,
            "bucket_ids": [bucket_id for bucket_id, _ in parts], "rows": rows}


def add_spend(db: Session, *, amount_minor: int, currency: str, bucket: models.Bucket, day: date | None = None,
              category: str | None = None, note: str | None = None) -> dict:
    """Record money spent out of one bucket. The caller commits."""
    day = day or user_clock.today()
    what = note or category or "spending"
    with change_log.batch_scope(db, "assistant"):
        entry = uuid.uuid4().hex
        row = _row(db, entry_id=entry, day=day, direction="out", minor=amount_minor, currency=currency,
                   bucket_id=bucket.id, category=category or "other", note=note,
                   summary=f"Spent {fmt(amount_minor, currency)} on {what} ({bucket.name})")
    return {"entry_id": entry, "amount_minor": amount_minor, "currency": currency, "bucket_ids": [bucket.id],
            "rows": [row]}


# ── The same money, once ─────────────────────────────────────

def _batch_income_entry(db: Session, amount_minor: int, currency: str, *, skip=(), unlinked: str | None = None):
    """An income entry written earlier in this batch for exactly this amount: the same
    money, said twice in one turn. ``unlinked`` names a column that must still be empty."""
    batch = change_log.current_batch_id(db)
    if not batch:
        return None
    rows = db.query(models.LedgerTransaction).filter(
        models.LedgerTransaction.batch_id == batch, models.LedgerTransaction.direction == "in",
        models.LedgerTransaction.currency == currency, models.LedgerTransaction.category != TRANSFER).all()
    entries: dict = {}
    for row in rows:
        entries.setdefault(row.entry_id, []).append(row)
    for entry_id, legs in entries.items():
        if entry_id in skip or sum(leg.amount_minor for leg in legs) != amount_minor:
            continue
        if unlinked and any(getattr(leg, unlinked) is not None for leg in legs):
            continue
        return legs
    return None


def _link(db: Session, legs, summary: str, **columns) -> None:
    for leg in legs:
        before = change_log.snapshot(leg)
        for name, value in columns.items():
            if value is not None and getattr(leg, name) is None:
                setattr(leg, name, value)
        change_log.record_update(db, leg, before, summary)


def on_payment_paid(db: Session, payment: models.Payment) -> None:
    """A client payment has just become paid: make sure the ledger holds that income
    exactly once, linked by ``payment_id``. Called by ``crm.mark_paid`` in the same batch."""
    if db.query(models.LedgerTransaction.id).filter(models.LedgerTransaction.payment_id == payment.id).first():
        return
    minor = float_to_minor(payment.amount)
    if minor <= 0:
        return
    currency = (payment.currency or models.default_currency()).upper()
    label = f"Linked to payment #{payment.id}"
    if payment.revenue_id is not None:
        # The revenue row was logged first (and its income with it); this payment is that money.
        legs = db.query(models.LedgerTransaction).filter(
            models.LedgerTransaction.revenue_id == payment.revenue_id).all()
        if legs:
            _link(db, legs, label, payment_id=payment.id)
            return
    legs = _batch_income_entry(db, minor, currency, unlinked="payment_id")
    if legs:
        _link(db, legs, label, payment_id=payment.id, revenue_id=payment.revenue_id)
        return
    client = db.query(models.Client).filter(models.Client.id == payment.client_id).first()
    add_income(db, amount_minor=minor, currency=currency, day=payment.paid_at or user_clock.today(),
               income_type="client", source=client.name if client else None,
               note=" · ".join(x for x in [client.name if client else None, payment.title] if x) or None,
               payment_id=payment.id, revenue_id=payment.revenue_id)


def on_revenue_logged(db: Session, revenue: models.Revenue) -> None:
    """Revenue was logged outside the CRM (a brain dump, the revenue form): the ledger gets
    that income once, linked by ``revenue_id``."""
    if db.query(models.LedgerTransaction.id).filter(models.LedgerTransaction.revenue_id == revenue.id).first():
        return
    minor = float_to_minor(revenue.amount or 0)
    if minor <= 0:
        return
    currency = (revenue.currency or models.default_currency()).upper()
    legs = _batch_income_entry(db, minor, currency, unlinked="revenue_id")
    if legs:
        _link(db, legs, f"Linked to revenue #{revenue.id}", revenue_id=revenue.id)
        return
    add_income(db, amount_minor=minor, currency=currency, day=revenue.date or user_clock.today(),
               income_type="business", source=revenue.source or None, revenue_id=revenue.id)


def sync_payment(db: Session, payment_id: int) -> None:
    """After an undo touched a payment: if it is no longer a paid payment, its income is
    no longer in the ledger either. (Undoing the whole batch has already removed it.)"""
    payment = db.query(models.Payment).filter(models.Payment.id == payment_id).first()
    if payment is not None and payment.status == "paid":
        return
    db.query(models.LedgerTransaction).filter(models.LedgerTransaction.payment_id == payment_id).delete()


# ── Reading ──────────────────────────────────────────────────

def balances(db: Session) -> dict:
    """``{(bucket id, currency): minor units}`` — computed, never stored."""
    signed = case((models.LedgerTransaction.direction == "in", models.LedgerTransaction.amount_minor),
                  else_=-models.LedgerTransaction.amount_minor)
    rows = db.query(models.LedgerTransaction.bucket_id, models.LedgerTransaction.currency,
                    func.coalesce(func.sum(signed), 0)).group_by(
        models.LedgerTransaction.bucket_id, models.LedgerTransaction.currency).all()
    return {(bucket_id, currency): int(total) for bucket_id, currency, total in rows}


def _balance_text(found: dict, bucket_id: int) -> str:
    mine = {cur: minor for (bid, cur), minor in found.items() if bid == bucket_id}
    if not mine:
        return fmt(0, models.default_currency())
    return " and ".join(fmt(minor, cur) for cur, minor in sorted(mine.items(), key=lambda kv: kv[0] != "ZMW"))


_PERIOD_WORDS = {
    "": "this_month", "month": "this_month", "this month": "this_month", "week": "this_week",
    "this week": "this_week", "year": "this_year", "this year": "this_year", "last month": "last_month",
    "all time": "all", "ever": "all", "last 7 days": "last_7_days", "last 30 days": "last_30_days",
}


def period_range(period, today: date | None = None) -> tuple[date | None, date | None, str]:
    """``(first day, day after the last, label)``. Open-ended for "all"."""
    today = today or user_clock.today()
    word = bucket_key(str(period or "").replace("_", " "))
    word = _PERIOD_WORDS.get(word, word.replace(" ", "_"))
    month_start = today.replace(day=1)
    next_month = (month_start + timedelta(days=32)).replace(day=1)
    if word == "this_month":
        return month_start, next_month, today.strftime("%B %Y")
    if word == "last_month":
        previous = (month_start - timedelta(days=1)).replace(day=1)
        return previous, month_start, previous.strftime("%B %Y")
    if word == "today":
        return today, today + timedelta(days=1), "today"
    if word == "yesterday":
        return today - timedelta(days=1), today, "yesterday"
    if word == "this_week":
        monday = today - timedelta(days=today.weekday())
        return monday, monday + timedelta(days=7), "this week"
    if word == "last_7_days":
        return today - timedelta(days=6), today + timedelta(days=1), "the last 7 days"
    if word == "last_30_days":
        return today - timedelta(days=29), today + timedelta(days=1), "the last 30 days"
    if word == "this_year":
        return date(today.year, 1, 1), date(today.year + 1, 1, 1), str(today.year)
    if word == "all":
        return None, None, "all time"
    found = re.fullmatch(r"(\d{4})-(\d{1,2})", word)
    if found and 1 <= int(found.group(2)) <= 12:
        first = date(int(found.group(1)), int(found.group(2)), 1)
        return first, (first + timedelta(days=32)).replace(day=1), first.strftime("%B %Y")
    raise ValueError(f"unknown period '{period}'. Use this_month, last_month, today, this_week, "
                     f"last_7_days, last_30_days, this_year, all, or a month like 2026-11")


def _in_period(query, start, end):
    if start is not None:
        query = query.filter(models.LedgerTransaction.occurred_at >= start)
    if end is not None:
        query = query.filter(models.LedgerTransaction.occurred_at < end)
    return query


def serialize(row: models.LedgerTransaction, names: dict, legs: int = 1) -> dict:
    return {
        "id": row.id, "entry_id": row.entry_id, "occurred_at": row.occurred_at, "direction": row.direction,
        "amount_minor": row.amount_minor, "amount": fmt(row.amount_minor, row.currency), "currency": row.currency,
        "bucket_id": row.bucket_id, "bucket": names.get(row.bucket_id), "category": row.category, "note": row.note,
        "income_type": row.income_type, "payment_id": row.payment_id, "revenue_id": row.revenue_id,
        "batch_id": row.batch_id, "transfer": row.category == TRANSFER, "legs": legs, "created_at": row.created_at,
    }


def transactions(db: Session, *, period=None, bucket_id: int | None = None, category: str | None = None,
                 direction: str | None = None, limit: int = 100, offset: int = 0) -> list[dict]:
    start, end, _ = period_range(period or "all")
    query = _in_period(db.query(models.LedgerTransaction), start, end)
    if bucket_id is not None:
        query = query.filter(models.LedgerTransaction.bucket_id == bucket_id)
    if category:
        query = query.filter(models.LedgerTransaction.category == bucket_key(category))
    if direction in ("in", "out"):
        query = query.filter(models.LedgerTransaction.direction == direction)
    rows = query.order_by(models.LedgerTransaction.occurred_at.desc(), models.LedgerTransaction.id.desc()
                          ).offset(offset).limit(limit).all()
    counts = dict(db.query(models.LedgerTransaction.entry_id, func.count(models.LedgerTransaction.id)).filter(
        models.LedgerTransaction.entry_id.in_({r.entry_id for r in rows})).group_by(
        models.LedgerTransaction.entry_id).all()) if rows else {}
    names = _names(db)
    return [serialize(r, names, counts.get(r.entry_id, 1)) for r in rows]


def rules_overview(db: Session) -> dict:
    names = _names(db)
    out: dict = {}
    for rule in db.query(models.AllocationRule).order_by(models.AllocationRule.id).all():
        out.setdefault(rule.income_type, []).append(
            {"bucket_id": rule.bucket_id, "bucket": names.get(rule.bucket_id), "pct_bp": rule.pct,
             "pct": pct_text(rule.pct)})
    return out


def summary(db: Session, period=None) -> dict:
    """Bucket balances (all time), and for the period: money in, money out and spending by
    category. Moves between buckets count as neither income nor spending."""
    start, end, label = period_range(period)
    found = balances(db)
    primary = models.default_currency()
    buckets = []
    for b in db.query(models.Bucket).order_by(models.Bucket.is_unallocated, models.Bucket.id).all():
        mine = {cur: minor for (bid, cur), minor in found.items() if bid == b.id} or {primary: 0}
        if b.is_unallocated and not any(mine.values()):
            continue                                   # nothing is waiting there: not worth a card
        for cur, minor in sorted(mine.items(), key=lambda kv: kv[0] != primary):
            buckets.append({"id": b.id, "name": b.name, "currency": cur, "balance_minor": minor,
                            "balance": fmt(minor, cur), "is_default": bool(b.is_default),
                            "is_remainder": bool(b.is_remainder), "is_unallocated": bool(b.is_unallocated)})
    real = _in_period(db.query(models.LedgerTransaction), start, end).filter(
        (models.LedgerTransaction.category.is_(None)) | (models.LedgerTransaction.category != TRANSFER))
    totals: dict = {}
    for direction, currency, total in real.with_entities(
            models.LedgerTransaction.direction, models.LedgerTransaction.currency,
            func.coalesce(func.sum(models.LedgerTransaction.amount_minor), 0)).group_by(
            models.LedgerTransaction.direction, models.LedgerTransaction.currency).all():
        totals.setdefault(currency, {"in": 0, "out": 0})[direction] = int(total)
    totals = totals or {primary: {"in": 0, "out": 0}}
    money = [{"currency": cur, "in_minor": t["in"], "out_minor": t["out"], "net_minor": t["in"] - t["out"],
              "in": fmt(t["in"], cur), "out": fmt(t["out"], cur), "net": fmt(t["in"] - t["out"], cur)}
             for cur, t in sorted(totals.items(), key=lambda kv: kv[0] != primary)]
    categories = [{"category": category or "other", "currency": currency, "total_minor": int(total),
                   "total": fmt(int(total), currency), "count": int(count)}
                  for category, currency, total, count in real.filter(models.LedgerTransaction.direction == "out")
                  .with_entities(models.LedgerTransaction.category, models.LedgerTransaction.currency,
                                 func.coalesce(func.sum(models.LedgerTransaction.amount_minor), 0),
                                 func.count(models.LedgerTransaction.id))
                  .group_by(models.LedgerTransaction.category, models.LedgerTransaction.currency).all()]
    categories.sort(key=lambda c: -c["total_minor"])
    return {"period": {"key": str(period or "this_month"), "label": label, "start": start, "end": end},
            "buckets": buckets, "money": money, "by_category": categories, "rules": rules_overview(db),
            "has_rules": bool(db.query(models.AllocationRule.id).first()),
            "transactions_total": db.query(func.count(models.LedgerTransaction.id)).scalar() or 0}


# ── The tools ────────────────────────────────────────────────

def _direction(item: dict) -> str | None:
    word = bucket_key(item.get("type") or item.get("direction") or "")
    if word in _OUT_WORDS:
        return "out"
    if word in _IN_WORDS:
        return "in"
    return None


def _same_batch_payment(db: Session, client_id: int, amount_minor: int, currency: str):
    """A paid payment from this client for this amount written earlier in the same batch."""
    made = [c["record_id"] for c in change_log.current_batch_changes(db)
            if c["table"] == "payments" and c["action"] == "create"]
    if not made:
        return None
    for p in db.query(models.Payment).filter(models.Payment.id.in_(made)).all():
        if (p.client_id == client_id and p.status == "paid" and (p.currency or "").upper() == currency
                and float_to_minor(p.amount) == amount_minor):
            return p
    return None


def log_transactions(db: Session, items) -> tuple[dict, str]:
    """Log everything he said he spent or received, as one batch. Nothing is written when
    anything in the list cannot be read or a bucket is unclear: he is asked once instead."""
    import crm
    import business_common
    import orchestrator_tools

    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list) or not items:
        return {"error": "nothing to log: give a list of items"}, "nothing to log"

    plan, today = [], user_clock.today()
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            return {"error": f"item {index} is not an object"}, f"item {index} is not an object"
        direction = _direction(item)
        if direction is None:
            msg = f"item {index}: say whether it was spent or received"
            return {"error": msg}, msg
        try:
            minor, currency = parse_money(item.get("amount"), item.get("currency"))
        except ValueError as exc:
            return {"error": f"item {index}: {exc}"}, f"item {index}: {exc}"
        day = today
        if item.get("date"):
            day = business_common.parse_day(item["date"], today, past=True)
            if day is None:
                msg = f"item {index}: could not read the date '{item['date']}'"
                return {"error": msg}, msg
        note = " ".join(str(item.get("note") or "").split()) or None
        step = {"direction": direction, "minor": minor, "currency": currency, "day": day, "note": note,
                "category": item.get("category"), "what": fmt(minor, currency)}
        if direction == "out":
            bucket, question = _spend_bucket(db, item.get("bucket"))
            if bucket is None:
                thing = f" for {note or item.get('category')}" if (note or item.get("category")) else ""
                question = f"The {step['what']}{thing}: {question[0].lower()}{question[1:]}"
                return ({"not_logged": "Nothing was written.", "question": question,
                         "next_step": "Ask Bandile exactly this question, then log the whole list again with his answer."},
                        f"asked: {question}")
            step["bucket"] = bucket
        else:
            source = " ".join(str(item.get("from") or "").split()) or None
            step["source"], step["income_type"] = source, item.get("income_type")
            if source:
                owner, scored = crm.find_client(db, source)
                if owner is not None:
                    step["client"] = owner
                elif scored and len(scored) > 1:
                    names = ", ".join(orchestrator_tools._client_label(c) for _score, c in scored[:3])
                    question = f"Which client is '{source}': {names}?"
                    return ({"not_logged": "Nothing was written.", "question": question,
                             "next_step": "Ask Bandile exactly this question, then log the whole list again."},
                            f"asked: {question}")
        plan.append(step)

    lines, touched, made, problems = [], [], set(), []
    first_unallocated = False
    with change_log.batch_scope(db, "assistant"):
        had_unallocated = _unallocated_income_count(db)
        for step in plan:
            what, tail = step["what"], (f" on {step['note']}" if step["note"] else "")
            if step["direction"] == "out":
                out = add_spend(db, amount_minor=step["minor"], currency=step["currency"], bucket=step["bucket"],
                                day=step["day"], category=step["category"], note=step["note"])
                touched += out["bucket_ids"]
                lines.append(f"Spent {what}{tail} from {step['bucket'].name}")
                continue
            source = step.get("source")
            if step.get("client") is not None:
                client = step["client"]
                existing = _same_batch_payment(db, client.id, step["minor"], step["currency"])
                if existing is None:
                    result, text = orchestrator_tools.record_payment(
                        db, client=str(client.id), amount=float(Decimal(step["minor"]) / 100),
                        currency=step["currency"], status="paid", title=step["note"])
                    if isinstance(result, dict) and result.get("error"):
                        problems.append(f"{what} from {client.name}: {result['error']}")
                        continue
                    payment_id = result["id"]
                    lines.append(f"Received {what} from {client.name} (client payment"
                                 f"{', settling what was due' if result.get('settled_existing') else ''})")
                else:
                    payment_id = existing.id
                    lines.append(f"{what} from {client.name} was already recorded this turn")
                legs = db.query(models.LedgerTransaction).filter(
                    models.LedgerTransaction.payment_id == payment_id).all()
                touched += [leg.bucket_id for leg in legs]
                made.update(leg.entry_id for leg in legs)
                if legs and all(leg.bucket_id == getattr(unallocated_bucket(db, create=False), "id", None)
                                for leg in legs):
                    first_unallocated = first_unallocated or not had_unallocated
                continue
            if _batch_income_entry(db, step["minor"], step["currency"], skip=made):
                lines.append(f"{what}{(' from ' + source) if source else ''} was already recorded this turn")
                continue
            kind = income_type_key(step.get("income_type"))
            revenue_id = None
            if kind in BUSINESS_TYPES:
                # Money the business earned from someone who is not a client on file: the
                # revenue scoreboard gets it too, as it always has.
                revenue = models.Revenue(amount=float(Decimal(step["minor"]) / 100), currency=step["currency"],
                                         source=source or step["note"] or "income", client_id=None, date=step["day"],
                                         notes="logged with the ledger")
                db.add(revenue)
                change_log.record_create(db, revenue, f"Revenue {what} from {revenue.source}")
                revenue_id = revenue.id
            out = add_income(db, amount_minor=step["minor"], currency=step["currency"], day=step["day"],
                             income_type=kind, source=source, note=step["note"], category=step["category"],
                             revenue_id=revenue_id)
            made.add(out["entry_id"])
            touched += out["bucket_ids"]
            if out["unallocated"]:
                first_unallocated = first_unallocated or not had_unallocated
            lines.append(f"Received {what}{(' from ' + source) if source else ''}")
        db.commit()

    names, found = _names(db), balances(db)
    order = list(dict.fromkeys(touched))
    now = {names[bid]: _balance_text(found, bid) for bid in order if bid in names}
    result: dict = {"logged": lines, "balances_now": now}
    if now:
        result["say"] = "Logged. " + ", ".join(f"{name} {amount}" for name, amount in now.items()) + " left."
        result["next_step"] = "Reply in ONE line: what was logged and each of these balances. Add nothing else."
    overdrawn = [name for bid in order for name in [names.get(bid)]
                 if any(minor < 0 for (b, _cur), minor in found.items() if b == bid)]
    if overdrawn:
        result["note"] = f"{' and '.join(overdrawn)} {'is' if len(overdrawn) == 1 else 'are'} below zero."
    if problems:
        result["not_logged"] = problems
    if first_unallocated and not db.query(models.AllocationRule.id).first():
        result["ask_once"] = ("There is no split for income yet, so it is sitting in Unallocated. Ask Bandile once, "
                              "in the same line, how he wants income split (for example '10 tithe, 40 business, "
                              "20 invest, 10 save, 20 personal').")
    return result, "; ".join(lines + problems) or "nothing logged"


def _unallocated_income_count(db: Session) -> int:
    bucket = unallocated_bucket(db, create=False)
    if bucket is None:
        return 0
    return db.query(func.count(models.LedgerTransaction.id)).filter(
        models.LedgerTransaction.bucket_id == bucket.id, models.LedgerTransaction.direction == "in").scalar() or 0


def _rule_pairs(rules) -> list[tuple[str, int]]:
    if isinstance(rules, dict):
        rules = [{"bucket": name, "pct": pct} for name, pct in rules.items()]
    if not isinstance(rules, list) or not rules:
        raise ValueError("give the split as a list of buckets and percentages")
    pairs, seen = [], set()
    for rule in rules:
        if not isinstance(rule, dict) or not bucket_key(rule.get("bucket")):
            raise ValueError("each part of the split needs a bucket and a percentage")
        name = " ".join(str(rule["bucket"]).split())
        if _stem(name) in seen:
            raise ValueError(f"'{name}' is in the split twice")
        seen.add(_stem(name))
        pairs.append((name, parse_pct(rule.get("pct"))))
    total = sum(points for _, points in pairs)
    if total != FULL:
        gap = FULL - total
        raise ValueError(f"that adds up to {pct_text(total)}, not 100%: "
                         f"{'where does the other ' + pct_text(gap) + ' go' if gap > 0 else 'it is ' + pct_text(-gap) + ' over'}?")
    return pairs


def set_allocation_rules(db: Session, rules, income_type=None, remainder_bucket=None, default_bucket_name=None,
                         split_unallocated: bool = False) -> tuple[dict, str]:
    """Set how income of a type is split. Creates the buckets that do not exist yet,
    replaces the earlier rules of that type, and (on his yes) splits what is waiting in
    Unallocated."""
    try:
        pairs = _rule_pairs(rules) if rules else None
    except ValueError as exc:
        return {"error": str(exc)}, str(exc)
    kind = income_type_key(income_type)
    if pairs is None and not split_unallocated and not remainder_bucket and not default_bucket_name:
        return {"error": "give the split, as buckets and percentages"}, "no split given"
    result: dict = {}
    with change_log.batch_scope(db, "assistant"):
        if pairs is not None:
            for old in db.query(models.AllocationRule).filter(models.AllocationRule.income_type == kind).all():
                change_log.record_delete(db, old, f"Old split for {kind} income removed")
            created = []
            for name, points in pairs:
                existed = find_bucket(db, name) is not None
                bucket = ensure_bucket(db, name)
                if not existed:
                    created.append(bucket.name)
                rule = models.AllocationRule(income_type=kind, bucket_id=bucket.id, pct=points,
                                             created_at=datetime.utcnow())
                db.add(rule)
                change_log.record_create(db, rule, f"{pct_text(points)} of {kind} income to {bucket.name}")
            if created:
                result["buckets_created"] = created
        if remainder_bucket:
            target = find_bucket(db, remainder_bucket)
            if target is None:
                db.rollback()
                msg = f"there is no '{remainder_bucket}' bucket to take the remainder"
                return {"error": msg}, msg
            _mark_only(db, target, "is_remainder", f"{target.name} takes the ngwee a split leaves over")
        elif pairs is not None and remainder_bucket_id(db) is None:
            biggest = find_bucket(db, max(pairs, key=lambda p: p[1])[0])
            _mark_only(db, biggest, "is_remainder", f"{biggest.name} takes the ngwee a split leaves over")
        if default_bucket_name:
            target = find_bucket(db, default_bucket_name)
            if target is None:
                db.rollback()
                msg = f"there is no '{default_bucket_name}' bucket to spend from"
                return {"error": msg}, msg
            _mark_only(db, target, "is_default", f"Spending comes out of {target.name} unless he names a bucket")
        elif default_bucket(db) is None:
            personal = find_bucket(db, "personal")
            if personal is not None:
                _mark_only(db, personal, "is_default", f"Spending comes out of {personal.name} unless he names a bucket")
        moved = _split_unallocated(db, kind) if split_unallocated else []
        db.commit()

    names = _names(db)
    current = rules_for(db, kind)
    result["income_type"] = kind
    result["split"] = [f"{pct_text(r.pct)} {names.get(r.bucket_id)}" for r in current]
    remainder = db.query(models.Bucket).filter(models.Bucket.is_remainder.is_(True)).first()
    spend_from = default_bucket(db)
    if remainder is not None:
        result["remainder_bucket"] = remainder.name
    result["spending_comes_from"] = spend_from.name if spend_from else "no default: he will be asked each time"
    if moved:
        result["moved_from_unallocated"] = moved
    waiting = _unallocated_waiting(db)
    if waiting and current:
        result["waiting_in_unallocated"] = waiting
        result["next_step"] = (f"Ask Bandile once whether to split the {waiting} sitting in Unallocated by this "
                               f"split. On his yes, call set_allocation_rules with split_unallocated true.")
    summary_text = ("Income split: " + ", ".join(result["split"])) if pairs is not None else "Buckets updated"
    if moved:
        summary_text += f"; split {', '.join(moved)} from Unallocated"
    return result, summary_text


def _unallocated_waiting(db: Session) -> str | None:
    bucket = unallocated_bucket(db, create=False)
    if bucket is None:
        return None
    mine = {cur: minor for (bid, cur), minor in balances(db).items() if bid == bucket.id and minor > 0}
    return " and ".join(fmt(minor, cur) for cur, minor in mine.items()) or None


def _split_unallocated(db: Session, income_type: str) -> list[str]:
    """Move what is waiting in Unallocated into the buckets, by the rules. A move between
    buckets: it is neither income nor spending."""
    bucket = unallocated_bucket(db, create=False)
    rules = rules_for(db, income_type)
    if bucket is None or not rules:
        return []
    names, moved, today = _names(db), [], user_clock.today()
    for (bucket_id, currency), minor in sorted(balances(db).items()):
        if bucket_id != bucket.id or minor <= 0:
            continue
        entry = uuid.uuid4().hex
        _row(db, entry_id=entry, day=today, direction="out", minor=minor, currency=currency, bucket_id=bucket.id,
             category=TRANSFER, note="Split by the income rules", income_type=TRANSFER,
             summary=f"{fmt(minor, currency)} moved out of {bucket.name}")
        for target, part in split(minor, [(r.bucket_id, r.pct) for r in rules], remainder_bucket_id(db)):
            _row(db, entry_id=entry, day=today, direction="in", minor=part, currency=currency, bucket_id=target,
                 category=TRANSFER, note=f"From {bucket.name}", income_type=TRANSFER,
                 summary=f"{fmt(part, currency)} moved to {names[target]}")
        moved.append(fmt(minor, currency))
    return moved


def get_finance(db: Session, view: str = "balances", period=None, category: str | None = None,
                bucket: str | None = None) -> tuple[dict, str]:
    """Read the ledger: ``balances``, ``spending`` (by category for a period), ``summary``
    (both, with money in and out) or ``transactions``. Read-only."""
    view = bucket_key(view) or "balances"
    try:
        data = summary(db, period)
    except ValueError as exc:
        return {"error": str(exc)}, str(exc)
    label = data["period"]["label"]
    held = {b["name"]: b["balance"] for b in data["buckets"]} if len({b["currency"] for b in data["buckets"]}) <= 1 \
        else {f"{b['name']} ({b['currency']})": b["balance"] for b in data["buckets"]}
    if view == "balances":
        if not held:
            return {"balances": "No buckets yet. Nothing has been logged."}, "no buckets yet"
        return {"balances": held}, ", ".join(f"{name} {amount}" for name, amount in held.items())
    wanted = bucket_key(category) if category else None
    spent = [c for c in data["by_category"] if not wanted or c["category"] == wanted]
    spending = {c["category"]: c["total"] for c in spent} or f"Nothing spent{' on ' + wanted if wanted else ''} in {label}."
    money = {m["currency"]: {"in": m["in"], "out": m["out"], "net": m["net"]} for m in data["money"]}
    money = next(iter(money.values())) if len(money) == 1 else money
    if view == "spending":
        text = ", ".join(f"{c['category']} {c['total']}" for c in spent) or "nothing spent"
        return {"period": label, "spending_by_category": spending, "total_spent": data["money"][0]["out"]
                if not wanted and len(data["money"]) == 1 else None}, f"{label}: {text}"
    if view == "transactions":
        target = find_bucket(db, bucket) if bucket else None
        if bucket and target is None:
            msg = f"there is no '{bucket}' bucket"
            return {"error": msg}, msg
        rows = transactions(db, period=period or "this_month", category=category,
                            bucket_id=target.id if target else None, limit=30)
        lines = [f"{r['occurred_at']}: {'received' if r['direction'] == 'in' else 'spent'} {r['amount']}"
                 f"{' on ' + (r['note'] or r['category']) if r['direction'] == 'out' and (r['note'] or r['category']) else ''}"
                 f"{' (' + r['note'] + ')' if r['direction'] == 'in' and r['note'] else ''} · {r['bucket']}"
                 f" (transaction {r['id']})" for r in rows]
        return {"period": label, "transactions": lines or f"Nothing logged in {label}."}, f"{len(rows)} transaction(s)"
    if view == "summary":
        return ({"period": label, "balances": held or "No buckets yet.", "money": money,
                 "spending_by_category": spending,
                 "income_split": {k: [f"{r['pct']} {r['bucket']}" for r in v] for k, v in data["rules"].items()}
                 or "No split is set."}, f"{label}: in {data['money'][0]['in']}, out {data['money'][0]['out']}")
    msg = f"unknown view '{view}'. Use balances, spending, summary or transactions"
    return {"error": msg}, msg


# ── Endpoints ────────────────────────────────────────────────

class TransactionPatch(BaseModel):
    bucket_id: int | None = None
    category: str | None = None
    note: str | None = None
    amount: str | None = None
    occurred_at: date | None = None


def _transaction_or_404(db: Session, transaction_id: int) -> models.LedgerTransaction:
    row = db.query(models.LedgerTransaction).filter(models.LedgerTransaction.id == transaction_id).first()
    if row is None:
        raise HTTPException(404, "Transaction not found")
    return row


@router.get("/finance/summary")
def summary_endpoint(period: str = "this_month", db: Session = Depends(get_db)):
    try:
        return summary(db, period)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.get("/finance/transactions")
def transactions_endpoint(period: str = "all", bucket_id: int | None = None, category: str | None = None,
                          direction: str | None = None, limit: int = Query(100, ge=1, le=500),
                          offset: int = Query(0, ge=0), db: Session = Depends(get_db)):
    try:
        return transactions(db, period=period, bucket_id=bucket_id, category=category, direction=direction,
                            limit=limit, offset=offset)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.patch("/finance/transactions/{transaction_id}")
def patch_transaction(transaction_id: int, payload: TransactionPatch, db: Session = Depends(get_db)):
    row = _transaction_or_404(db, transaction_id)
    fields = payload.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(400, "Nothing to change")
    if row.category == TRANSFER and set(fields) - {"note"}:
        raise HTTPException(409, "A move between buckets cannot be edited. Undo it instead.")
    legs = db.query(func.count(models.LedgerTransaction.id)).filter(
        models.LedgerTransaction.entry_id == row.entry_id).scalar() or 1
    with change_log.batch_scope(db, "ui"):
        before = change_log.snapshot(row)
        names = _names(db)
        if "amount" in fields and fields["amount"] is not None:
            if row.payment_id is not None:
                raise HTTPException(409, "This is a client payment. Change the amount on the payment, in Clients.")
            if legs > 1:
                raise HTTPException(409, "This income is split across buckets. Undo it and log it again to change the amount.")
            try:
                row.amount_minor, row.currency = parse_money(fields["amount"], row.currency)
            except ValueError as exc:
                raise HTTPException(400, str(exc))
        if fields.get("bucket_id") is not None:
            if fields["bucket_id"] not in names:
                raise HTTPException(400, "Unknown bucket")
            row.bucket_id = fields["bucket_id"]
        if "category" in fields:
            if bucket_key(fields["category"]) == TRANSFER:
                raise HTTPException(400, "'transfer' is kept for moves between buckets")
            row.category = bucket_key(fields["category"]) or None
        if "note" in fields:
            row.note = (fields["note"] or "").strip()[:300] or None
        if fields.get("occurred_at") is not None:
            row.occurred_at = fields["occurred_at"]
        what = row.note or row.category or ("income" if row.direction == "in" else "spending")
        change = change_log.record_update(
            db, row, before, f"Edited {fmt(row.amount_minor, row.currency)} {what} ({names.get(row.bucket_id)})")
        db.commit()
    return {"transaction": serialize(row, names, legs), "change": change, "changes": [change] if change else []}


@router.delete("/finance/transactions/{transaction_id}")
def delete_transaction(transaction_id: int, db: Session = Depends(get_db)):
    """Remove the whole entry this row belongs to (every leg of a split income). The screen
    asks before calling this; it is change-logged and can be undone."""
    row = _transaction_or_404(db, transaction_id)
    legs = db.query(models.LedgerTransaction).filter(models.LedgerTransaction.entry_id == row.entry_id).all()
    if any(leg.payment_id is not None for leg in legs):
        raise HTTPException(409, "This is a client payment. Undo or cancel the payment instead, in Clients.")
    names = _names(db)
    with change_log.batch_scope(db, "ui"):
        changes = [change_log.record_delete(
            db, leg, f"Removed {fmt(leg.amount_minor, leg.currency)} {leg.note or leg.category or ''} "
                     f"({names.get(leg.bucket_id)})".replace("  ", " ")) for leg in legs]
        db.commit()
    return {"deleted": len(changes), "changes": changes}


@router.post("/finance/buckets/{bucket_id}/default")
def make_default(bucket_id: int, db: Session = Depends(get_db)):
    bucket = db.query(models.Bucket).filter(models.Bucket.id == bucket_id).first()
    if bucket is None or bucket.is_unallocated:
        raise HTTPException(404, "Bucket not found")
    with change_log.batch_scope(db, "ui"):
        _mark_only(db, bucket, "is_default", f"Spending comes out of {bucket.name} unless he names a bucket")
        changes = change_log.current_batch_changes(db)
        db.commit()
    return {"bucket": {"id": bucket.id, "name": bucket.name, "is_default": True}, "changes": changes}
