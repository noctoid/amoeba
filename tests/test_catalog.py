import pytest

from amoeba import Catalog, Table, infer_backend, load_catalog

CONFIG = """\
[[tables]]
name = "users"
source = "data/users.csv"

[[tables.columns]]
name = "id"
type = "BIGINT"

[[tables.columns]]
name = "name"
type = "VARCHAR"

[[tables]]
name = "events"
source = "events.parquet"
backend = "parquet"
"""


def test_load_catalog_lookup(tmp_path):
    path = tmp_path / "amoeba.toml"
    path.write_text(CONFIG)
    cat = load_catalog(path)
    assert isinstance(cat.table("users"), Table)
    assert isinstance(cat.table("events"), Table)
    assert cat.table("missing") is None
    assert len(cat.tables) == 2


def test_backend_inferred_and_overridden(tmp_path):
    path = tmp_path / "amoeba.toml"
    path.write_text(CONFIG)
    cat = load_catalog(path)
    assert cat.table("users").backend == "csv"
    assert cat.table("events").backend == "parquet"


def test_schema_overrides_only_explicit(tmp_path):
    path = tmp_path / "amoeba.toml"
    path.write_text(
        """\
[[tables]]
name = "t"
source = "a.csv"

[[tables.columns]]
name = "id"
type = "BIGINT"

[[tables.columns]]
name = "note"
"""
    )
    cat = load_catalog(path)
    assert cat.table("t").schema_overrides == {"id": "BIGINT"}


@pytest.mark.parametrize(
    ("source", "backend"),
    [
        ("data/a.csv", "csv"),
        ("data/a.xlsx", "xlsx"),
        ("DATA/A.CSV", "csv"),
        ("data/a.parquet", "parquet"),
    ],
)
def test_infer_backend(source, backend):
    assert infer_backend(source) == backend


def test_unknown_extension_raises():
    with pytest.raises(ValueError, match="cannot infer backend"):
        infer_backend("data/weird.xyz")


def test_duplicate_table_raises(tmp_path):
    path = tmp_path / "amoeba.toml"
    path.write_text(
        """\
[[tables]]
name = "t"
source = "a.csv"

[[tables]]
name = "t"
source = "b.csv"
"""
    )
    with pytest.raises(ValueError, match="duplicate table name"):
        load_catalog(path)


def test_missing_source_raises(tmp_path):
    path = tmp_path / "amoeba.toml"
    path.write_text('[[tables]]\nname = "t"\n')
    with pytest.raises(ValueError, match="missing required key 'source'"):
        load_catalog(path)
