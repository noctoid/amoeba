"""Amoeba MySQL server: bind a catalog to the MySQL wire protocol."""

from __future__ import annotations

from mysql_mimic import MysqlServer

from .catalog import Catalog
from .session import AmoebaSession


def make_server(catalog: Catalog, *, database: str = "amoeba", **kwargs) -> MysqlServer:
    """Build a ``MysqlServer`` whose sessions serve ``catalog``."""

    def factory() -> AmoebaSession:
        return AmoebaSession(catalog, database=database)

    return MysqlServer(session_factory=factory, **kwargs)


async def serve(
    catalog: Catalog,
    *,
    host: str = "127.0.0.1",
    port: int = 3306,
    database: str = "amoeba",
) -> None:
    """Run the server until cancelled."""
    server = make_server(catalog, database=database)
    await server.start_server(host=host, port=port)
    print(f"amoeba listening on {host}:{port} (database {database!r})")
    await server.serve_forever()
