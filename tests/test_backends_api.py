"""API backend tests: JSON pull, pagination, auth headers, guards.

A threaded ``http.server`` plays the API; each test registers a route
handler ``(query_params, request_headers) -> (status, json_body)``.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from amoeba import ApiConfig, Catalog, Column, Engine, Table, load_catalog, scan


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        route = self.server.routes.get(parsed.path)
        if route is None:
            status, body = 404, {"error": "no such route"}
        else:
            self.server.requests.append(parsed.path)
            status, body = route(parse_qs(parsed.query), self.headers)
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):  # keep test output clean
        pass


@pytest.fixture
def api_server():
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    server.routes = {}
    server.requests = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()
    thread.join()


def _url(server, path="/items"):
    return f"http://127.0.0.1:{server.server_port}{path}"


def _table(server, **kwargs):
    kwargs.setdefault("source", _url(server))
    kwargs.setdefault("backend", "api")
    kwargs.setdefault("name", "t")
    return Table(**kwargs)


def test_rows_path_extracts_nested_array(api_server, duckdb_conn):
    api_server.routes["/items"] = lambda q, h: (
        200,
        {"meta": {"total": 2}, "items": [{"id": 1}, {"id": 2}]},
    )
    table = _table(api_server, api=ApiConfig(rows_path="$.items"))
    assert scan(table, duckdb_conn) == [{"id": 1}, {"id": 2}]


def test_root_array_with_default_path(api_server, duckdb_conn):
    api_server.routes["/items"] = lambda q, h: (200, [{"id": 1}])
    assert scan(_table(api_server), duckdb_conn) == [{"id": 1}]


def test_offset_pagination_collects_all_pages(api_server, duckdb_conn):
    rows = [{"id": i} for i in range(5)]

    def route(query, headers):
        limit = int(query["limit"][0])
        offset = int(query["offset"][0])
        return 200, {"items": rows[offset : offset + limit]}

    api_server.routes["/items"] = route
    table = _table(api_server, api=ApiConfig(rows_path="$.items", pagination="offset", page_size=2))
    assert scan(table, duckdb_conn) == rows
    assert len(api_server.requests) == 3  # 2 + 2 + short final page


def test_cursor_pagination_follows_tokens(api_server, duckdb_conn):
    pages = {
        None: ({"id": 1}, "tok-b"),
        "tok-b": ({"id": 2}, "tok-c"),
        "tok-c": ({"id": 3}, None),
    }

    def route(query, headers):
        row, nxt = pages[query.get("cursor", [None])[0]]
        return 200, {"data": [row], "next_cursor": nxt}

    api_server.routes["/items"] = route
    table = _table(
        api_server,
        api=ApiConfig(rows_path="$.data", pagination="cursor", cursor_path="$.next_cursor"),
    )
    assert scan(table, duckdb_conn) == [{"id": 1}, {"id": 2}, {"id": 3}]


def test_static_auth_headers_are_sent(api_server, duckdb_conn):
    seen = {}

    def route(query, headers):
        seen["auth"] = headers.get("Authorization")
        return 200, [{"id": 1}]

    api_server.routes["/items"] = route
    table = _table(api_server, api=ApiConfig(headers=(("Authorization", "Bearer s3cret"),)))
    scan(table, duckdb_conn)
    assert seen["auth"] == "Bearer s3cret"


def test_http_error_raises_with_status(api_server, duckdb_conn):
    api_server.routes["/items"] = lambda q, h: (500, {"error": "boom"})
    with pytest.raises(ValueError, match="HTTP 500"):
        scan(_table(api_server), duckdb_conn)


def test_max_pages_refuses_unbounded_pull(api_server, duckdb_conn):
    api_server.routes["/items"] = lambda q, h: (
        200,
        {"items": [{"id": 1}, {"id": 2}]},
    )
    table = _table(
        api_server,
        api=ApiConfig(rows_path="$.items", pagination="offset", page_size=2, max_pages=3),
    )
    with pytest.raises(ValueError, match="max_pages"):
        scan(table, duckdb_conn)


def test_rows_path_must_resolve(api_server, duckdb_conn):
    api_server.routes["/items"] = lambda q, h: (200, {"other": []})
    table = _table(api_server, api=ApiConfig(rows_path="$.items"))
    with pytest.raises(ValueError, match="not found in API response"):
        scan(table, duckdb_conn)


def test_rows_must_be_objects(api_server, duckdb_conn):
    api_server.routes["/items"] = lambda q, h: (200, {"items": [1, 2, 3]})
    table = _table(api_server, api=ApiConfig(rows_path="$.items"))
    with pytest.raises(ValueError, match="expected an object"):
        scan(table, duckdb_conn)


def test_rows_path_must_be_an_array(api_server, duckdb_conn):
    api_server.routes["/items"] = lambda q, h: (200, {"items": {"id": 1}})
    table = _table(api_server, api=ApiConfig(rows_path="$.items"))
    with pytest.raises(ValueError, match="expected an array"):
        scan(table, duckdb_conn)


def test_engine_queries_api_table(api_server):
    api_server.routes["/items"] = lambda q, h: (
        200,
        {"items": [{"id": 1, "name": "alice"}, {"id": 2, "name": "bob"}]},
    )
    table = Table(
        name="users",
        source=_url(api_server),
        backend="api",
        api=ApiConfig(rows_path="$.items"),
    )
    eng = Engine(Catalog(tables=(table,)))
    assert eng.execute("SELECT COUNT(*) FROM users")[0] == [(2,)]
    rows, cols = eng.execute("SELECT name FROM users WHERE id = 2")
    assert rows == [("bob",)]
    assert [c.name for c in cols] == ["name"]
    eng.close()


def test_empty_api_response_uses_declared_columns(api_server):
    api_server.routes["/items"] = lambda q, h: (200, {"items": []})
    table = Table(
        name="empty_api",
        source=_url(api_server),
        backend="api",
        columns=(Column("id", "BIGINT"), Column("name", "VARCHAR")),
        api=ApiConfig(rows_path="$.items"),
    )
    eng = Engine(Catalog(tables=(table,)))
    assert eng.execute("SELECT COUNT(*) FROM empty_api")[0] == [(0,)]
    assert eng.mysql_schema()["empty_api"] == {"id": "bigint", "name": "varchar"}
    eng.close()


def test_empty_api_response_without_columns_raises(api_server):
    api_server.routes["/items"] = lambda q, h: (200, {"items": []})
    nope = _table(api_server, name="nope", api=ApiConfig(rows_path="$.items"))
    with pytest.raises(ValueError, match="cannot infer a schema"):
        Engine(Catalog(tables=(nope,)))


def test_catalog_parses_api_table(tmp_path):
    path = tmp_path / "amoeba.toml"
    path.write_text(
        """\
[[tables]]
name = "issues"
backend = "api"
source = "https://api.example.com/issues"
rows_path = "$.items"
pagination = "offset"
page_size = 50

[tables.headers]
Authorization = "Bearer tok"
"""
    )
    table = load_catalog(path).table("issues")
    assert table.api.rows_path == "$.items"
    assert table.api.pagination == "offset"
    assert table.api.page_size == 50
    assert table.api.headers == (("Authorization", "Bearer tok"),)
    assert table.api.max_pages == 100  # default guard


def test_catalog_rejects_bad_pagination(tmp_path):
    path = tmp_path / "amoeba.toml"
    path.write_text(
        """\
[[tables]]
name = "t"
backend = "api"
source = "https://api.example.com/x"
pagination = "sideways"
"""
    )
    with pytest.raises(ValueError, match="unknown pagination"):
        load_catalog(path)


def test_catalog_rejects_api_keys_on_file_backend(tmp_path):
    path = tmp_path / "amoeba.toml"
    path.write_text(
        """\
[[tables]]
name = "t"
source = "data.csv"
pagination = "offset"
"""
    )
    with pytest.raises(ValueError, match='require backend = "api"'):
        load_catalog(path)


def test_catalog_rejects_non_http_api_source(tmp_path):
    path = tmp_path / "amoeba.toml"
    path.write_text(
        """\
[[tables]]
name = "t"
backend = "api"
source = "ftp://example.com/x"
"""
    )
    with pytest.raises(ValueError, match="http"):
        load_catalog(path)
