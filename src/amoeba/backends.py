"""Backend adapters: materialize a table source into plain rows.

The adapter boundary returns ``list[dict]`` — one dict per row, keyed by
column name. The engine converts these to Arrow and registers them with
DuckDB. Adapters never hand-build Arrow; the DuckDB reads below are the
engine's own native file readers behind the uniform ``scan`` contract.
"""

from __future__ import annotations

from pathlib import Path

import duckdb

from .catalog import Table

#: Backend name → DuckDB table function that reads one source path.
_READERS: dict[str, str] = {
    "csv": "read_csv_auto",
    "parquet": "read_parquet",
    "xlsx": "read_xlsx",
}


def reader(table: Table) -> str:
    """DuckDB table function name for ``table``'s backend."""
    try:
        return _READERS[table.backend]
    except KeyError:
        raise ValueError(
            f"table {table.name!r}: unknown backend {table.backend!r}"
        ) from None


def source_path(table: Table) -> str:
    """Absolute path of ``table``'s source, as DuckDB should open it."""
    return str(Path(table.source).resolve())


def scan(table: Table, conn: duckdb.DuckDBPyConnection) -> list[dict]:
    """Read ``table.source`` into a list of dicts, one per row."""
    cur = conn.execute(f"SELECT * FROM {reader(table)}(?)", [source_path(table)])
    names = [d[0] for d in cur.description]
    return [dict(zip(names, row)) for row in cur.fetchall()]
