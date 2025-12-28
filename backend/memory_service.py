from sqlalchemy.orm import Session
from models import AIMemory


def get_recent_memories(
    db: Session,
    limit: int = 5,
    context_type: str | None = None
):
    query = db.query(AIMemory)

    if context_type:
        query = query.filter(AIMemory.context_type == context_type)

    return (
        query
        .order_by(AIMemory.created_at.desc())
        .limit(limit)
        .all()
    )
