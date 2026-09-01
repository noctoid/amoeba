"""Amoeba: MySQL-protocol-compatible SQL over files and APIs."""

__version__ = "0.1.0"
from .backends import scan
from .catalog import Catalog, Column, Table, ApiConfig, infer_backend, load_catalog
from .engine import Engine
from .session import AmoebaSession

__all__ = [
    "AmoebaSession",
    "ApiConfig",
    "Catalog",
    "Column",
    "Engine",
    "Table",
    "infer_backend",
    "load_catalog",
    "scan",
    "__version__",
]
