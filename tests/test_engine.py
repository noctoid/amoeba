"""Engine-level tests: registration, execution, type mapping, overrides."""

import openpyxl
import pyarrow as pa
import pyarrow.parquet as pq

from amoeba import Catalog, Column, Engine, Table


def _make_catalog(tmp_path, *, name="users", rows, columns=()):
    src = tmp_path / f"{name}.csv"
    src.write_text(rows)
    return Catalog(tables=(Table(name=name, source=str(src), backend="csv", columns=columns),))


USERS = "id,name,score\n1,alice,1.5\n2,bob,2.5\n3,carol,3.5\n"


def test_select_and_count(tmp_path):
    eng = Engine(_make_catalog(tmp_path, rows=USERS))
    rows, cols = eng.execute("SELECT * FROM users ORDER BY id")
    assert rows == [(1, "alice", 1.5), (2, "bob", 2.5), (3, "carol", 3.5)]
    assert [c.name for c in cols] == ["id", "name", "score"]
    assert [c.type.name for c in cols] == ["LONGLONG", "VARCHAR", "DOUBLE"]
    assert eng.execute("SELECT COUNT(*) FROM users")[0] == [(3,)]
    eng.close()


def test_schema_map(tmp_path):
    eng = Engine(_make_catalog(tmp_path, rows=USERS))
    assert eng.mysql_schema() == {
        "users": {"id": "bigint", "name": "varchar", "score": "double"}
    }
    eng.close()


def test_schema_override_applies_cast(tmp_path):
    eng = Engine(_make_catalog(tmp_path, rows=USERS, columns=(Column("id", "VARCHAR"),)))
    rows, cols = eng.execute("SELECT * FROM users ORDER BY id")
    assert rows[0][0] == "1"
    assert cols[0].type.name == "VARCHAR"
    # Override shows in the catalog, and the internal table is not leaked.
    assert eng.mysql_schema()["users"]["id"] == "varchar"
    assert set(eng.mysql_schema()) == {"users"}
    eng.close()


def test_empty_table_has_schema(tmp_path):
    eng = Engine(_make_catalog(tmp_path, name="empty", rows="id,name\n"))
    assert eng.execute("SELECT COUNT(*) FROM empty")[0] == [(0,)]
    assert set(eng.mysql_schema()["empty"]) == {"id", "name"}
    eng.close()


def test_parquet_backend(tmp_path):
    path = tmp_path / "p.parquet"
    pq.write_table(pa.Table.from_pylist([{"id": 1, "v": "x"}, {"id": 2, "v": "y"}]), path)
    eng = Engine(Catalog(tables=(Table(name="p", source=str(path), backend="parquet"),)))
    assert eng.execute("SELECT * FROM p ORDER BY id")[0] == [(1, "x"), (2, "y")]
    eng.close()


def test_xlsx_backend(tmp_path):
    path = tmp_path / "x.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["id", "v"])
    ws.append([1, "x"])
    ws.append([2, "y"])
    wb.save(path)
    eng = Engine(Catalog(tables=(Table(name="x", source=str(path), backend="xlsx"),)))
    assert eng.execute("SELECT * FROM x ORDER BY id")[0] == [(1, "x"), (2, "y")]
    eng.close()
