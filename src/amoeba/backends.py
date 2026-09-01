"""Backend adapters: materialize a table source into plain rows.

The adapter boundary returns ``list[dict]`` — one dict per row, keyed by
column name. The engine converts these to Arrow and registers them with
DuckDB. Adapters never hand-build Arrow; the DuckDB reads below are the
engine's own native file readers behind the uniform ``scan`` contract.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa

from .catalog import ApiConfig, Table

#: Backend name → DuckDB table function that reads one source path.
_READERS: dict[str, str] = {
    "csv": "read_csv_auto",
    "parquet": "read_parquet",
    "xlsx": "read_xlsx",
}


def reader(table: Table) -> str:
    """DuckDB table function name for ``table``'s file backend."""
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
    if table.backend == "api":
        return _scan_api(table)
    cur = conn.execute(f"SELECT * FROM {reader(table)}(?)", [source_path(table)])
    names = [d[0] for d in cur.description]
    return [dict(zip(names, row)) for row in cur.fetchall()]


def empty_arrow(table: Table, conn: duckdb.DuckDBPyConnection) -> pa.Table:
    """Zero-row Arrow table carrying ``table``'s schema.

    File backends ask the reader for its inferred schema. API tables have
    nothing to infer from an empty response, so the schema is built from
    the declared column types — the only honest source when there is no
    data.
    """
    if table.backend == "api":
        overrides = table.schema_overrides
        if not overrides:
            raise ValueError(
                f"table {table.name!r}: API returned no rows and no column "
                f"types are declared; cannot infer a schema"
            )
        cols = ", ".join(
            f'CAST(NULL AS {t}) AS "{n}"' for n, t in overrides.items()
        )
        return conn.execute(f"SELECT {cols} WHERE false").arrow().read_all()
    cur = conn.execute(
        f"SELECT * FROM {reader(table)}(?) LIMIT 0", [source_path(table)]
    )
    return cur.arrow().read_all()


def _scan_api(table: Table) -> list[dict]:
    """Pull all rows from a JSON HTTP API, following its pagination."""
    cfg = table.api or ApiConfig()
    rows: list[dict] = []
    cursor: str | None = None
    for page in range(cfg.max_pages):
        params: dict[str, str] = {}
        if cfg.pagination == "offset":
            params[cfg.limit_param] = str(cfg.page_size)
            params[cfg.offset_param] = str(page * cfg.page_size)
        elif cfg.pagination == "cursor" and cursor:
            params[cfg.cursor_param] = cursor
        payload = _get_json(table.source, params, cfg)
        batch = _resolve(payload, cfg.rows_path, table.name)
        if not isinstance(batch, list):
            raise ValueError(
                f"table {table.name!r}: rows_path {cfg.rows_path!r} resolved "
                f"to a {type(batch).__name__}, expected an array"
            )
        for item in batch:
            if not isinstance(item, dict):
                raise ValueError(
                    f"table {table.name!r}: API row is a "
                    f"{type(item).__name__}, expected an object"
                )
        rows.extend(batch)
        if cfg.pagination == "none":
            return rows
        if cfg.pagination == "offset":
            if len(batch) < cfg.page_size:
                return rows
        else:  # cursor
            cursor = _resolve_or_none(payload, cfg.cursor_path)
            if not cursor:
                return rows
    raise ValueError(
        f"table {table.name!r}: exceeded max_pages={cfg.max_pages}; "
        f"refusing an unbounded pull"
    )


def _get_json(url: str, params: dict[str, str], cfg: ApiConfig) -> Any:
    """GET ``url`` with query params and static headers; parse the JSON body."""
    if params:
        sep = "&" if "?" in url else "?"
        url = url + sep + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=dict(cfg.headers))
    try:
        with urllib.request.urlopen(req, timeout=cfg.timeout_s) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        raise ValueError(
            f"API request failed: HTTP {e.code} from {url.split('?', 1)[0]}"
        ) from None
    except urllib.error.URLError as e:
        raise ValueError(
            f"API request failed: {e.reason} ({url.split('?', 1)[0]})"
        ) from None


def _resolve(payload: Any, path: str, table: str) -> Any:
    """Walk a dotted ``$.a.b.0`` path into parsed JSON."""
    node = payload
    for seg in _path_segments(path):
        if isinstance(node, dict) and seg in node:
            node = node[seg]
        elif isinstance(node, list) and seg.isdigit() and int(seg) < len(node):
            node = node[int(seg)]
        else:
            raise ValueError(
                f"table {table!r}: path {path!r} not found in API response"
            )
    return node


def _resolve_or_none(payload: Any, path: str) -> Any | None:
    """``_resolve`` for optional values (next-page cursors): missing → None."""
    try:
        return _resolve(payload, path, "cursor")
    except ValueError:
        return None


def _path_segments(path: str) -> list[str]:
    p = path.strip()
    if p.startswith("$"):
        p = p[1:]
    if p.startswith("."):
        p = p[1:]
    return p.split(".") if p else []
