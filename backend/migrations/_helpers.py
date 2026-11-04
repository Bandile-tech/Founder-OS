"""
Shared helpers for hand-written, re-runnable migrations (m010 onwards).

Every helper is idempotent and works on both SQLite and Postgres. Migrations
must never rely on ``create_all`` for new columns: the startup schema check
would crash-loop Render if a column were missing.
"""

from sqlalchemy import text


def dialect(conn, tag: str) -> str:
    name = getattr(conn.dialect, "name", None)
    if name in ("sqlite", "postgresql"):
        return name
    raise RuntimeError(f"[{tag}] Unsupported dialect '{name}'.")


def table_exists(conn, dialect_name: str, table: str) -> bool:
    if dialect_name == "postgresql":
        row = conn.execute(text("""
            SELECT COUNT(*) FROM information_schema.tables
            WHERE table_schema = 'public' AND table_name = :t
        """), {"t": table}).scalar()
        return bool(row)
    rows = conn.execute(
        text("SELECT name FROM sqlite_master WHERE type='table' AND name=:t"), {"t": table}
    ).fetchall()
    return bool(rows)


def column_exists(conn, dialect_name: str, table: str, column: str) -> bool:
    if dialect_name == "postgresql":
        row = conn.execute(text("""
            SELECT COUNT(*) FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = :t AND column_name = :c
        """), {"t": table, "c": column}).scalar()
        return bool(row)
    rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
    return column in {r[1] for r in rows}


def add_column_if_missing(conn, dialect_name: str, table: str, column: str,
                          type_sql: str, tag: str) -> bool:
    """ADD COLUMN when absent. Returns True if the column was added."""
    if not table_exists(conn, dialect_name, table):
        print(f"[{tag}] {table} does not exist yet — create_all will build it with {column}.")
        return False
    if column_exists(conn, dialect_name, table, column):
        return False
    if dialect_name == "postgresql":
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {type_sql}"))
    else:
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {type_sql}"))
    print(f"[{tag}] Added {table}.{column}")
    return True


def create_table_if_missing(conn, dialect_name: str, table: str, sql: str, tag: str) -> bool:
    """``sql`` may contain ``{autoincrement}`` which is filled per dialect."""
    if table_exists(conn, dialect_name, table):
        return False
    autoincrement = "AUTOINCREMENT" if dialect_name == "sqlite" else ""
    conn.execute(text(sql.format(autoincrement=autoincrement)))
    print(f"[{tag}] Created {table}")
    return True


def create_index(conn, name: str, table: str, columns: str, unique: bool = False) -> None:
    kw = "UNIQUE INDEX" if unique else "INDEX"
    conn.execute(text(f"CREATE {kw} IF NOT EXISTS {name} ON {table} ({columns})"))


def enable_rls(conn, dialect_name: str, tables: list[str], tag: str) -> None:
    """Postgres only: RLS on + no policies = anon/PostgREST access denied (m008 convention)."""
    if dialect_name != "postgresql":
        return
    for table in tables:
        conn.execute(text(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY"))
        print(f"[{tag}]   RLS enabled: {table}")
