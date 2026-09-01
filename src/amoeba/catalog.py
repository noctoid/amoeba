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


_PAGINATION_STYLES = ("none", "offset", "cursor")

#: Config keys that only make sense for the ``api`` backend.
_API_KEYS = (
    "rows_path",
    "pagination",
    "limit_param",
    "offset_param",
    "page_size",
    "cursor_param",
    "cursor_path",
    "max_pages",
    "timeout_s",
    "headers",
)


@dataclass(frozen=True)
class ApiConfig:
    """How to pull rows from a JSON HTTP API (``backend = "api"``).

    ``rows_path`` is a dotted path (``$.items``, ``$`` for a top-level
    array) to the array of row objects in the response. Pagination styles:

    - ``none``   — one request returns everything.
    - ``offset`` — ``limit_param``/``offset_param`` query params, paged by
      ``page_size`` until a short page comes back.
    - ``cursor`` — the response carries the next token at ``cursor_path``;
      it is sent back as ``cursor_param`` until absent/empty.

    ``max_pages`` is a hard cap — the refusal of the unbounded pull from
    §7.3. ``headers`` carries static auth (``Authorization``, API keys).
    """

    rows_path: str = "$"
    pagination: str = "none"
    limit_param: str = "limit"
    offset_param: str = "offset"
    page_size: int = 100
    cursor_param: str = "cursor"
    cursor_path: str = "$.next_cursor"
    max_pages: int = 100
    timeout_s: float = 30.0
    headers: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class Table:
    """A virtual table declared over one source."""

    name: str
    source: str
    backend: str
    columns: tuple[Column, ...] = ()
    api: ApiConfig | None = None

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
        if backend == "api":
            scheme = source.split("://", 1)[0].lower()
            if scheme not in ("http", "https"):
                raise ValueError(
                    f"table {name!r}: api source must be an http(s) URL, "
                    f"got {source!r}"
                )
        tables.append(
            Table(
                name=name,
                source=source,
                backend=backend,
                columns=_parse_columns(raw.get("columns", []), name),
                api=_parse_api(raw, name, backend),
            )
        )
    return Catalog(tables=tuple(tables))


def _parse_api(raw: dict, table: str, backend: str) -> ApiConfig | None:
    present = {k: raw[k] for k in _API_KEYS if k in raw}
    if backend != "api":
        if present:
            raise ValueError(
                f"table {table!r}: keys {sorted(present)} require "
                f'backend = "api"'
            )
        return None
    pagination = present.get("pagination", "none")
    if pagination not in _PAGINATION_STYLES:
        raise ValueError(
            f"table {table!r}: unknown pagination {pagination!r}; "
            f"expected one of {', '.join(_PAGINATION_STYLES)}"
        )
    headers = present.get("headers", {})
    if not isinstance(headers, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in headers.items()
    ):
        raise ValueError(f"table {table!r}: headers must be a string table")
    return ApiConfig(
        rows_path=present.get("rows_path", "$"),
        pagination=pagination,
        limit_param=present.get("limit_param", "limit"),
        offset_param=present.get("offset_param", "offset"),
        page_size=int(present.get("page_size", 100)),
        cursor_param=present.get("cursor_param", "cursor"),
        cursor_path=present.get("cursor_path", "$.next_cursor"),
        max_pages=int(present.get("max_pages", 100)),
        timeout_s=float(present.get("timeout_s", 30.0)),
        headers=tuple(headers.items()),
    )


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
