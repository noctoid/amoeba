"""End-to-end wire-protocol tests: real MySQL client against the server."""

import asyncio
import threading

import pymysql
import pytest

from amoeba import Catalog, Table
from amoeba.server import make_server


@pytest.fixture(scope="module")
def server_port(tmp_path_factory):
    src = tmp_path_factory.mktemp("data") / "users.csv"
    src.write_text("id,name,score\n1,alice,1.5\n2,bob,2.5\n3,carol,3.5\n")
    catalog = Catalog(tables=(Table(name="users", source=str(src), backend="csv"),))

    started = threading.Event()
    state: dict = {}

    def run() -> None:
        async def main() -> None:
            server = make_server(catalog)
            await server.start_server(host="127.0.0.1", port=0)
            state["port"] = server.sockets()[0].getsockname()[1]
            state["server"] = server
            started.set()
            await server.serve_forever()

        asyncio.run(main())

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert started.wait(10), "server did not start"
    yield state["port"]
    state["server"].close()


@pytest.fixture()
def conn(server_port):
    c = pymysql.connect(
        host="127.0.0.1", port=server_port, user="root", password="", autocommit=True
    )
    yield c
    c.close()


def _rows(cur):
    return cur.fetchall()


def test_select_all(conn):
    cur = conn.cursor()
    cur.execute("SELECT * FROM users ORDER BY id")
    assert [c[0] for c in cur.description] == ["id", "name", "score"]
    assert _rows(cur) == ((1, "alice", 1.5), (2, "bob", 2.5), (3, "carol", 3.5))


def test_count(conn):
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM users")
    assert _rows(cur) == ((3,),)


def test_metadata(conn):
    cur = conn.cursor()
    cur.execute("SHOW DATABASES")
    dbs = {r[0] for r in _rows(cur)}
    assert "amoeba" in dbs

    cur.execute("SHOW TABLES")
    assert _rows(cur) == (("users",),)

    cur.execute("DESCRIBE users")
    desc = {r[0]: r[1] for r in _rows(cur)}
    assert desc == {"id": "bigint", "name": "varchar", "score": "double"}


def test_filter_order_limit(conn):
    cur = conn.cursor()
    cur.execute("SELECT id, score FROM users WHERE score > 2 ORDER BY score DESC LIMIT 1")
    assert _rows(cur) == ((3, 3.5),)
