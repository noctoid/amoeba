"""Amoeba: MySQL-protocol-compatible SQL over files and APIs."""

__version__ = "0.1.0"
from .catalog import Catalog, Column, Table, infer_backend, load_catalog

__all__ = [
    "Catalog",
    "Column",
    "Table",
    "infer_backend",
    "load_catalog",
    "__version__",
]
