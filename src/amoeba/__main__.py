"""CLI entrypoint: ``amoeba path/to/amoeba.toml``."""

from __future__ import annotations

import argparse
import asyncio

from .catalog import load_catalog
from .server import serve


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="amoeba",
        description="Serve a TOML catalog as a MySQL-protocol SQL endpoint.",
    )
    parser.add_argument("catalog", help="path to the catalog TOML file")
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=3306, help="bind port (default 3306)")
    parser.add_argument("--database", default="amoeba", help="database name (default 'amoeba')")
    args = parser.parse_args(argv)

    catalog = load_catalog(args.catalog)
    try:
        asyncio.run(serve(catalog, host=args.host, port=args.port, database=args.database))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
