"""Package shim — keeps `from tools.sap_import import SapImportWindow` working after split."""

from .window import SapImportWindow

__all__ = ["SapImportWindow"]
