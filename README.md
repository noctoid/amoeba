# Ameoba

A MySQL-protocol-compatible service that converts SQL queries into an abstract
intermediate representation (IR) and adapts it to arbitrary "backends" — CSV/XLSX
files, Parquet/Arrow files, and any API that returns table-like data.

> This document is a brainstorm capture. It records the design discussion, the
> existing landscape, the known difficulties, and the fundamental limits. It is
> not an implementation plan yet.

---

## 1. Concept / Problem statement

Expose many heterogeneous data sources (files, columnar formats, REST APIs) behind
a single MySQL-compatible endpoint, so that off-the-shelf clients — MySQL CLI,
JDBC, PyMySQL — can query them with ordinary SQL without knowing or caring what
the backend actually is.

The server translates:

```
MySQL wire protocol request
        │
        ▼
   SQL (text or prepared statement)
        │
        ▼
   Abstract IR (logical plan + Arrow data)
        │
        ▼
   Backend adapter (CSV / XLSX / Parquet / Arrow / HTTP API / ...)
```

## 2. Goal

- Accept connections from MySQL CLI, JDBC, and PyMySQL clients.
- Parse SQL into a backend-agnostic intermediate representation.
- Route execution to pluggable backends: `.csv`, `.xlsx`, `.parquet`/Arrow, and
  APIs returning table-like data.
- Do this for "simple SQL" — project, filter, join, aggregate, sort, limit, insert.

## 3. Non-goals (v1)

- Full MySQL feature parity (stored procedures, triggers, foreign keys, FULLTEXT,
  spatial, replication/binlog, `LOAD DATA LOCAL INFILE`).
- True ACID transactions over non-transactional backends.
- Drop-in replacement for arbitrary existing MySQL workloads.

## 4. Existing landscape

This is a well-trodden shape. Every serious project below is the same three
pieces: wire protocol + SQL engine + storage adapters.

| Project | Protocol | Notes |
|---|---|---|
| MariaDB **CONNECT** engine | Native MySQL/MariaDB | `ENGINE=CONNECT TABLE_TYPE=CSV/JSON/XML/INI/DBF` maps files to tables; Excel via `ODBC`/`JDBC`; `$`-marked types can be REST results. Closest off-the-shelf match. |
| **ClickHouse** | MySQL wire protocol (`mysql_port`) | `file()`, `url()`, `s3()`, `input()` table functions read CSV/JSON/Parquet; `INSERT INTO FUNCTION` writes files. Dialect ≠ full MySQL. |
| **Steampipe** | Postgres (not MySQL) | SQL → live API calls via plugins (200+ services). Canonical "SQL → RPC" pattern. Also SQLite extension + MCP server. |
| **go-mysql-server** (DoltHub) | MySQL wire protocol + full engine | Pluggable `sql.DatabaseProvider`/`sql.Table` storage interface. The closest single-package analog to this project's design. |
| **Apache Calcite** | Framework | Planner/optimizer + relational IR; you implement the execution + frontend. CSV adapter example exists. |
| **Apache DataFusion** | Library (Arrow-native) | Logical/physical plan, Arrow record batches, `TableProvider` with pushdown capability flags. |
| **Trino/Presto** | HTTP (not MySQL) | Connector SPI = the "adapter" concept, with explicit capability/limit negotiation. |
| **DuckDB** | Embedded | Reads CSV/Parquet/JSON/Excel; `ATTACH` to MySQL/PG/SQLite; `register()` pandas/Arrow. |

## 5. Proposed architecture

Three layers:

1. **Protocol layer** — MySQL wire protocol: handshake, auth, `COM_QUERY`,
   `COM_STMT_PREPARE`/`EXECUTE`, text + binary result sets, error packets,
   metadata, `INFORMATION_SCHEMA`.
2. **Engine layer** — SQL parser → **logical plan (query IR)** → executor that
   computes join/aggregate/subquery/window/DISTINCT/GROUP BY/type coercion.
3. **Adapter layer** — per-backend implementation of the IR, with capability
   flags; the engine fills any gap the backend can't do.

### 5.1 The three IRs

"Intermediate representation" is actually three distinct things; conflating them
causes most of the pain:

- **Query IR** — a logical plan (project/filter/join/aggregate/sort/limit with
  typed columns). This is what adapters consume via a pushdown contract.
- **Data IR** — the rows flowing between adapters and the engine. **Arrow** is the
  right choice: columnar, typed, zero-copy, and Parquet is an Arrow/IPC
  serialization (so the Parquet backend cost collapses to near zero).
- **Catalog IR** — schema/table/column metadata served as `INFORMATION_SCHEMA` /
  `SHOW`.

### 5.2 Adapter contract

Capability-flagged interface, not "convert query to RPC":

```
scan(plan, projection, filters, limit)
  supports: Filter | Project | Sort | Limit | Aggregate | Join
            Write | Update | Delete | Transaction | PredicatePushdown
```

A backend that can't do `WHERE`/`ORDER BY`/`LIMIT` → the engine materializes and
computes locally. The failure mode is not "impossible," it is "unbounded data you
must pull."

This is precisely Trino's `ConnectorMetadata` + `ConnectorSplit`, DataFusion's
`TableProvider` + `supports_filters_pushdown`, and Calcite's
`RelOptTable`/`EnumerableConvention`.

## 6. Difficulties (the risk register)

Roughly in order of what actually bites:

1. **Driver conformance is stricter than it looks.** JDBC/ORM frameworks emit real
   introspection SQL before the user's query runs:
   `SELECT @@version_comment`, `SELECT VERSION()`, `SET NAMES utf8mb4`,
   `SHOW TABLES/COLUMNS/VARIABLES`, `INFORMATION_SCHEMA.tables/columns` reads,
   `BEGIN/COMMIT/ROLLBACK`, server-side prepared statements (`COM_STMT_PREPARE` +
   binary result protocol), multi-result sets, streaming. "Just implement SELECT"
   is not enough.
2. **You are building a database, not a proxy.** The engine (parser → logical plan
   → executor) is unavoidable; files and APIs have no SQL executor of their own.
3. **Typing is lossy in both directions.** MySQL `TINYINT(1)`=bool, `DECIMAL`
   precision, `DATETIME` vs `TIMESTAMP`, `ENUM`/`SET`, `BLOB`; JDBC reads
   `ResultSetMetaData.getColumnType()`. CSV/JSON have no types at all.
4. **Writes/mutations are semantically hard.** CSV append-only-ish; XLSX writes
   rewrite the whole workbook; Parquet/Arrow are immutable (update/delete =
   rewrite + atomic swap); API `INSERT`/`UPDATE`/`DELETE` may not exist or be
   non-idempotent.
5. **Transactions/isolation don't exist on files/APIs.** `BEGIN; ...; COMMIT;`
   cannot be made atomic over a CSV or stateless HTTP API — only emulated.
6. **Session state, concurrency, streaming.** Per-connection variables,
   `LAST_INSERT_ID()`, temp tables, `USE db`, prepared-statement cache, charset;
   large result sets + `ORDER BY`/`GROUP BY` require full materialization, which
   conflicts with streaming/paginated backends.

## 7. Fundamental limits (cannot be done "anyhow")

1. **ACID over non-transactional backends.** No real atomicity/isolation/durability
   for multi-row writes against CSV/XLSX/immutable Parquet/stateless APIs. Emulate,
   not guarantee.
2. **Lossless type recovery from typeless sources.** CSV/JSON string `"123"` has no
   provenance; you cannot know it is an integer without a supplied schema.
3. **Arbitrary pushdown to capability-less backends.** If the backend can't filter
   and the data is large, `SELECT * WHERE x=1` requires downloading everything; if
   the API has no pagination/snapshot, some queries are **not executable at all**.
4. **In-place mutation of immutable formats.** Parquet/Arrow update/delete must
   rewrite whole files; no single-row atomic write.
5. **Full MySQL parity without reimplementing MySQL.** "Simple SQL" and "drop-in
   for arbitrary MySQL clients" are mutually exclusive; scope must be stated.

## 8. Implementation paths

- **Go** — `go-mysql-server`: protocol + engine + storage interface in one package.
  Least total work if Go is acceptable.
- **Rust + Arrow** — DataFusion: logical/physical plan, Arrow data IR, pushdown
  capability flags; add a MySQL wire shim. Best for columnar/Parquet/Arrow and
  max control.
- **Java** — Calcite + Avatica: planner/optimizer + relational IR; add a MySQL
  protocol frontend.

## 9. Python path (notes)

No single Python package equals `go-mysql-server`; it is a composition, and the
serious engines are C++/Rust cores with Python bindings.

- **`mysql-mimic`** — pure-Python MySQL wire protocol (`pip install mysql-mimic`).
  Subclass `Session.query(expression, sql, attrs)` → return rows+columns;
  `schema()` serves `INFORMATION_SCHEMA`/`SHOW`. Built on **sqlglot** for parsing;
  pre-handles metadata/`@@variable`/`USE` traffic.
  - **Maintenance assessment (2026-08-28):** actively maintained — releases
    v3.0.3 (2026-04-03) and v3.0.4 (2026-05-05), real bug fixes (Python 3.13 /
    uvloop), 2 open issues, not archived. But: **small single-maintainer project**
    (122 stars, 6 watchers), self-classified "Alpha", positioned as a
    protocol-simulation/mocking tool. PyPI metadata (author `kelsin`) is stale
    relative to the canonical `barakalon/mysql-mimic` repo. Verify
    `COM_STMT_PREPARE`/binary-protocol fidelity against the actual drivers.
- **Engine options:** DuckDB (`read_csv`/`read_parquet`/`read_excel`/`register`,
  `ATTACH`) for least effort; **DataFusion-Python** (`pip install datafusion`,
  Apache) for the real IR/adapter architecture (Substrait = serialized IR); Polars
  and **sqlglot.executor** as lighter/limited alternatives; **Ibis** as the
  "abstract expression IR → many backends" library.
- **Python-specific limits:** custom-API pushdown is awkward in both DuckDB
  `register()` and DataFusion-Python (thin `TableProvider` surface vs Rust); GIL +
  asyncio marshaling bottleneck at the Python↔engine boundary; the compiled cores
  do the heavy lifting.

## 10. Decisions

Resolved 2026-08-29:

- [x] **Target client class: thin clients** (mysql CLI, PyMySQL, light GUI
      clients). No JDBC/ORM conformance burden — full `INFORMATION_SCHEMA` /
      server-side prepared statement fidelity is out of scope for v1.
- [x] **Language: Python** — `mysql-mimic` (protocol) + **DuckDB** (engine) +
      **pyarrow** (data IR glue). DuckDB reads CSV/Parquet/Excel natively and
      executes the SQL; adapters feed it.
- [x] **Data IR: Arrow internally, `list[dict]` at the adapter boundary.**
      Adapters return plain `list[dict]`; the engine boundary converts with
      `pyarrow.Table.from_pylist()` and registers into DuckDB. Adapters never
      hand-build Arrow.
- [x] **Write/mutation scope: read-only v1.** Simple `INSERT`/`UPDATE` are in
      the v1+ SQL definition but deferred; the adapter contract leaves room
      for them.
- [x] **Schema/typing policy: both, composed.** Inference is the default
      (pyarrow/DuckDB sniffing); explicit per-table schema overrides take
      precedence and are applied as casts at registration time. Rule:
      *override wins, infer the rest.* This is also the escape hatch for the
      fundamental limit §7.2.
- [x] **"Simple SQL" (v1 definition):** `SELECT * FROM t`,
      `SELECT COUNT(*) FROM t` (i.e. plain projection + basic aggregates),
      simple `INSERT`, simple `UPDATE`. DuckDB's own parser/planner is the
      engine, so joins/aggregates beyond this come for free; the definition
      bounds what we *commit to supporting*, not what happens to work.

### Architecture consequence

With DuckDB as the engine, the formal "logical plan IR" is DuckDB's own
planner, not one we control. The adapter contract is thin for v1:

```
scan(table) -> list[dict]          # plus optional filter/projection hints later
```

Revisit DataFusion-Python the day real predicate pushdown into custom APIs
matters.

## 10.1 Milestones

1. [x] **Project scaffold** — package layout, `pyproject.toml`, deps
   (`mysql-mimic`, `duckdb`, `pyarrow`, `openpyxl`).
2. [x] **Config-driven catalog** — declare `my_table -> data.csv`, optional
   schema override.
3. [x] **Wire protocol → engine** — `mysql-mimic` session executes via DuckDB.
4. [x] **End-to-end proof** — real `mysql` CLI: `SELECT * FROM my_table;` and
   `SELECT COUNT(*) FROM my_table;`.

XLSX backend and the first API adapter follow once the spine is proven.

### Run it

```console
$ amoeba path/to/amoeba.toml --port 3307
$ mysql --protocol=tcp -h 127.0.0.1 -P 3307 -u root
mysql> SHOW TABLES;
mysql> SELECT * FROM users;
```

The server accepts any username with an empty password (mock auth).

## 11. References

- MariaDB CONNECT table types: <https://mariadb.com/kb/en/connect-table-types-overview/>
- ClickHouse MySQL interface: <https://clickhouse.com/docs/en/interfaces/mysql>
- Steampipe: <https://steampipe.io/docs>
- go-mysql-server: <https://github.com/dolthub/go-mysql-server>
- DataFusion (Rust): <https://github.com/apache/datafusion>
- DataFusion Python: <https://github.com/apache/datafusion-python>
- Calcite: <https://calcite.apache.org/>
- mysql-mimic: <https://github.com/barakalon/mysql-mimic>
- sqlglot: <https://github.com/tobymao/sqlglot>
