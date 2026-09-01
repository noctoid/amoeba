"""DuckDB execution engine: register catalog tables, run SQL, map types."""

from __future__ import annotations

import duckdb
import pyarrow as pa

from mysql_mimic.results import ResultColumn
from mysql_mimic.types import ColumnType

from . import backends
from .catalog import Catalog, Table

#: DuckDB type name → mysql-mimic ``ColumnType`` (result-set wire encoding).
_DUCKDB_TO_COLUMN_TYPE: dict[str, ColumnType] = {
    "BOOLEAN": ColumnType.TINY,
    "TINYINT": ColumnType.TINY,
    "UTINYINT": ColumnType.TINY,
    "SMALLINT": ColumnType.SHORT,
    "USMALLINT": ColumnType.SHORT,
    "INTEGER": ColumnType.LONG,
    "UINTEGER": ColumnType.LONG,
    "BIGINT": ColumnType.LONGLONG,
    "UBIGINT": ColumnType.LONGLONG,
    "HUGEINT": ColumnType.LONGLONG,
    "UHUGEINT": ColumnType.LONGLONG,
    "FLOAT": ColumnType.FLOAT,
    "REAL": ColumnType.FLOAT,
    "DOUBLE": ColumnType.DOUBLE,
    "DECIMAL": ColumnType.DECIMAL,
    "VARCHAR": ColumnType.VARCHAR,
    "CHAR": ColumnType.STRING,
    "BPCHAR": ColumnType.STRING,
    "TEXT": ColumnType.VARCHAR,
    "STRING": ColumnType.VARCHAR,
    "BLOB": ColumnType.BLOB,
    "BYTEA": ColumnType.BLOB,
    "DATE": ColumnType.DATE,
    "TIME": ColumnType.TIME,
    "TIMETZ": ColumnType.TIME,
    "TIMESTAMP": ColumnType.DATETIME,
    "TIMESTAMP WITH TIME ZONE": ColumnType.DATETIME,
    "TIMESTAMP_S": ColumnType.DATETIME,
    "TIMESTAMP_MS": ColumnType.DATETIME,
    "TIMESTAMP_NS": ColumnType.DATETIME,
    "JSON": ColumnType.JSON,
    "INTERVAL": ColumnType.VARCHAR,
    "UUID": ColumnType.VARCHAR,
    "ENUM": ColumnType.STRING,
}

#: DuckDB type name → MySQL type name (INFORMATION_SCHEMA / SHOW COLUMNS).
_DUCKDB_TO_MYSQL_NAME: dict[str, str] = {
    "BOOLEAN": "tinyint",
    "TINYINT": "tinyint",
    "UTINYINT": "tinyint",
    "SMALLINT": "smallint",
    "USMALLINT": "smallint",
    "INTEGER": "int",
    "UINTEGER": "int",
    "BIGINT": "bigint",
    "UBIGINT": "bigint",
    "HUGEINT": "bigint",
    "UHUGEINT": "bigint",
    "FLOAT": "float",
    "REAL": "float",
    "DOUBLE": "double",
    "DECIMAL": "decimal",
    "VARCHAR": "varchar",
    "CHAR": "char",
    "BPCHAR": "char",
    "TEXT": "text",
    "STRING": "varchar",
    "BLOB": "blob",
    "BYTEA": "blob",
    "DATE": "date",
    "TIME": "time",
    "TIMETZ": "time",
    "TIMESTAMP": "datetime",
    "TIMESTAMP WITH TIME ZONE": "datetime",
    "TIMESTAMP_S": "datetime",
    "TIMESTAMP_MS": "datetime",
    "TIMESTAMP_NS": "datetime",
    "JSON": "json",
    "INTERVAL": "varchar",
    "UUID": "varchar",
    "ENUM": "varchar",
}


def _base_type(dtype: object) -> str:
    """Normalize a DuckDB type (object or string) to its bare name."""
    return str(dtype).split("(", 1)[0].strip().upper()

class Engine:
    """A DuckDB connection with every catalog table registered."""

    def __init__(self, catalog: Catalog) -> None:
        self._conn = duckdb.connect(database=":memory:")
        for table in catalog.tables:
            self._register(table)

    def execute(self, sql: str) -> tuple[list, list[ResultColumn]]:
        """Run one statement; return ``(rows, result_columns)``."""
        cur = self._conn.execute(sql)
        columns = [
            ResultColumn(name, _DUCKDB_TO_COLUMN_TYPE.get(_base_type(dtype), ColumnType.VARCHAR))
            for name, dtype in ((d[0], d[1]) for d in cur.description or [])
        ]
        return cur.fetchall(), columns

    def mysql_schema(self) -> dict[str, dict[str, str]]:
        """Map ``{table: {column: mysql_type}}`` for INFORMATION_SCHEMA."""
        rows = self._conn.execute(
            "SELECT table_name, column_name, data_type "
            "FROM information_schema.columns "
            "ORDER BY table_name, ordinal_position"
        ).fetchall()
        schema: dict[str, dict[str, str]] = {}
        for table, column, dtype in rows:
            if table.startswith("_amoeba_"):
                continue
            schema.setdefault(table, {})[column] = _DUCKDB_TO_MYSQL_NAME.get(
                _base_type(dtype), "varchar"
            )
        return schema

    def close(self) -> None:
        self._conn.close()

    def _register(self, table: Table) -> None:
        rows = backends.scan(table, self._conn)
        arrow = pa.Table.from_pylist(rows) if rows else backends.empty_arrow(table, self._conn)
        overrides = table.schema_overrides
        if not overrides:
            self._conn.register(table.name, arrow)
            return

        # Apply explicit schema overrides as DuckDB casts over the inferred
        # table, leaving every unlisted column untouched.
        internal = f"_amoeba_{table.name}"
        self._conn.register(internal, arrow)
        projection = ", ".join(
            f'CAST("{c}" AS {overrides[c]}) AS "{c}"' if c in overrides else f'"{c}"'
            for c in arrow.column_names
        )
        self._conn.execute(
            f'CREATE VIEW "{table.name}" AS SELECT {projection} FROM "{internal}"'
        )
