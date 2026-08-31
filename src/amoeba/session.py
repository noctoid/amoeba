"""mysql-mimic session: route SQL to the DuckDB engine."""

from __future__ import annotations

from typing import Any, Dict

import sqlglot.expressions as exp

from mysql_mimic.results import AllowedResult
from mysql_mimic.session import Session as BaseSession

from .catalog import Catalog
from .engine import Engine


class AmoebaSession(BaseSession):
    """Serve a :class:`Catalog` to a single MySQL client connection.

    Metadata traffic (``SHOW``, ``DESCRIBE``, ``SET``, ``USE``,
    ``INFORMATION_SCHEMA``, ``BEGIN``/``COMMIT``/``ROLLBACK``, static
    ``SELECT``s) is handled by ``BaseSession``'s middlewares; only real
    statements reach :meth:`query`, which executes them in DuckDB.
    """

    def __init__(self, catalog: Catalog, database: str = "amoeba") -> None:
        super().__init__()
        self._default_database = database
        self.database = database
        self._engine = Engine(catalog)

    @property
    def database(self) -> str:
        """Current database, falling back to the catalog's default.

        mysql-mimic overwrites ``session.database`` from the client's
        handshake (``None`` when no database is selected). Serve the default
        in that case so ``SHOW TABLES`` / ``DATABASE()`` work without ``USE``.
        """
        return self._database if self._database is not None else self._default_database

    @database.setter
    def database(self, value: str | None) -> None:
        self._database = value

    async def query(
        self, expression: exp.Expression, sql: str, attrs: Dict[str, str]
    ) -> AllowedResult:
        rows, columns = self._engine.execute(sql)
        return rows, columns

    async def schema(self) -> dict[str, dict[str, dict[str, str]]]:
        return {self.database: self._engine.mysql_schema()}

    async def close(self) -> None:
        await super().close()
        self._engine.close()
