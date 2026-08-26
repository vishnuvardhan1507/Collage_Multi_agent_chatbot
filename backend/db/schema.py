from typing import Optional

from tools.db_tool import get_connection


def get_database_schema(db_path: Optional[str] = None) -> str:
    """Return the current SQLite schema as table(column, ...) lines."""
    with get_connection(db_path) as conn:
        tables = conn.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table'
              AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            """
        ).fetchall()

        schema_lines = []
        for table in tables:
            table_name = table["name"]
            columns = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
            column_names = ", ".join(column["name"] for column in columns)
            schema_lines.append(f"{table_name}({column_names})")

    return "\n".join(schema_lines)
