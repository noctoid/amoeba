import duckdb
import pytest


@pytest.fixture
def duckdb_conn():
    conn = duckdb.connect(database=":memory:")
    yield conn
    conn.close()
