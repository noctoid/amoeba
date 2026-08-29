"""Config-driven catalog: declare virtual tables over files.

This is the Catalog IR — the schema/table/column metadata that later
milestones serve as ``SHOW TABLES`` / ``INFORMATION_SCHEMA`` and use to
drive adapter registration.

Column ``type`` values are DuckDB type strings (``BIGINT``, ``VARCHAR``,
``DECIMAL(10,2)``). ``None`` means "infer from the source" — the default
per the schema policy (override wins, infer the rest).
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

#: Source extension → backend name.
_BACKEND_BY_SUFFIX: dict[str, str] = {
    ".csv": "csv",
    ".tsv": "csv",
    ".xlsx": "xlsx",
    ".xls": "xlsx",
    ".parquet": "parquet",
    ".arrow": "parquet",
}


@dataclass(frozen=True)
class Column:
    """One column of a virtual table."""

    name: str
    type: str | None = None


@dataclass(frozen=True)
class Table:
    """A virtual table declared over one source."""

    name: str
    source: str
    backend: str
    columns: tuple[Column, ...] = ()

    @property
    def schema_overrides(self) -> dict[str, str]:
        """Explicit column types keyed by name (the cast map)."""
        return {c.name: c.type for c in self.columns if c.type is not None}


@dataclass(frozen=True)
class Catalog:
    """The set of virtual tables known to the server."""

    tables: tuple[Table, ...]

    def table(self, name: str) -> Table | None:
        for t in self.tables:
            if t.name == name:
                return t
        return None


def infer_backend(source: str) -> str:
    """Map a source path's extension to a backend name."""
    suffix = Path(source).suffix.lower()
    try:
        return _BACKEND_BY_SUFFIX[suffix]
    except KeyError:
        raise ValueError(
            f"cannot infer backend for {source!r}; "
            f"recognized suffixes: {', '.join(sorted(_BACKEND_BY_SUFFIX))}"
        ) from None


def load_catalog(path: str | Path) -> Catalog:
    """Parse a TOML catalog file into a :class:`Catalog`."""
    data = _read_config(path)
    tables: list[Table] = []
    seen: set[str] = set()
    for raw in data.get("tables", []):
        name = _require(raw, "name", "table")
        source = _require(raw, "source", f"table {name!r}")
        if name in seen:
            raise ValueError(f"duplicate table name {name!r}")
        seen.add(name)
        backend = raw.get("backend") or infer_backend(source)
        tables.append(
            Table(
                name=name,
                source=source,
                backend=backend,
                columns=_parse_columns(raw.get("columns", []), name),
            )
        )
    return Catalog(tables=tuple(tables))


def _parse_columns(raw_cols: list, table: str) -> tuple[Column, ...]:
    cols: list[Column] = []
    seen: set[str] = set()
    for raw in raw_cols:
        name = _require(raw, "name", f"table {table!r} column")
        if name in seen:
            raise ValueError(f"table {table!r}: duplicate column name {name!r}")
        seen.add(name)
        cols.append(Column(name=name, type=raw.get("type")))
    return tuple(cols)


def _require(raw: dict, key: str, where: str) -> str:
    value = raw.get(key)
    if value is None:
        raise ValueError(f"{where}: missing required key {key!r}")
    return value


def _read_config(path: str | Path) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)
