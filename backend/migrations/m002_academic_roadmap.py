"""
Migration 002 — Pre-seed academic subjects and topics.

Tables are created by SQLAlchemy's create_all() on startup; this script
only handles the data seed.  It is idempotent at the subject level:
each subject is checked by code and only inserted if missing, so
deleting an individual subject and restarting will re-seed it without
touching subjects that already exist.

Run directly:
    python migrations/002_academic_roadmap.py
Or import seed_subjects() from other scripts.
"""

import sys
from pathlib import Path
from datetime import date

# Allow importing from backend/ when run as a standalone script
sys.path.insert(0, str(Path(__file__).parent.parent))

from database import SessionLocal
from models import Subject, Topic


# ── Seed catalogue ────────────────────────────────────────────
# (code, name, exam_date, sort_order, [(topic_name, weight), ...])
SEED_DATA = [
    (
        "9709",
        "Maths 9709",
        date(2026, 11, 1),
        0,
        [
            ("Pure Mathematics 1 (P1)",           8),
            ("Probability & Statistics 1 (S1)",   7),
        ],
    ),
    (
        "9231",
        "Further Maths 9231",
        date(2026, 11, 1),
        1,
        [
            ("Further Pure Mathematics 1",             8),
            ("Further Probability & Statistics 1",     7),
        ],
    ),
    (
        "9609",
        "Business 9609",
        date(2026, 11, 1),
        2,
        [
            ("1.1 Enterprise",          5),
            ("1.2 Business Structure",  5),
            ("1.3 Size of Business",    5),
            ("1.4 Business Objectives", 6),
            ("1.5 Stakeholders",        5),
        ],
    ),
    (
        "9708",
        "Economics 9708",
        date(2026, 11, 1),
        3,
        [
            ("Basic economic ideas and the economic problem", 7),
            ("Resource allocation",                           7),
            ("The price system and the microeconomy",         8),
            ("Government microeconomic intervention",         7),
            ("International economic issues",                 6),
            ("Macroeconomic theory and policy",               7),
        ],
    ),
]


def seed_subjects(db=None) -> None:
    """
    Insert any missing subjects + their topics.
    Each subject is identified by code; if it already exists it is skipped.
    """
    close_after = db is None
    if db is None:
        db = SessionLocal()

    try:
        for code, name, exam_date, sort_order, topics in SEED_DATA:
            existing = db.query(Subject).filter(Subject.code == code).first()
            if existing:
                print(f"[seed] subject '{code}' already exists — skipping")
                continue

            subj = Subject(
                name=name,
                code=code,
                exam_date=exam_date,
                sort_order=sort_order,
            )
            db.add(subj)
            db.flush()  # get subj.id before adding topics

            for i, (topic_name, weight) in enumerate(topics):
                db.add(Topic(
                    subject_id=subj.id,
                    name=topic_name,
                    syllabus_weight=weight,
                    sort_order=i,
                ))

            print(f"[seed] seeded subject '{name}' with {len(topics)} topics")

        db.commit()
    finally:
        if close_after:
            db.close()


if __name__ == "__main__":
    seed_subjects()
    print("Migration 002 complete.")
