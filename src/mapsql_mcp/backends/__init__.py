"""Database backends. ``get_spec`` / ``create_service`` pick one from the settings (see ``Settings.backend_name``)."""

from __future__ import annotations

from ..config import Settings
from ..errors import MapsqlError
from .base import BackendSpec, SpatialService


def spec_by_name(name: str) -> BackendSpec:
    if name == "oracle":
        from .oracle_spec import SPEC
    else:
        from .postgis import SPEC
    return SPEC


def get_spec(settings: Settings) -> BackendSpec:
    return spec_by_name(settings.backend_name)


def create_service(settings: Settings) -> SpatialService:
    """Instantiate the service for the configured backend (Oracle dependencies are imported lazily)."""
    if settings.backend_name == "oracle":
        try:
            from .oracle import OracleService
        except ImportError as e:  # pragma: no cover - depends on the environment
            raise MapsqlError(
                "The Oracle backend needs extra packages: pip install 'mapsql-mcp[oracle]' "
                f"(python-oracledb, sqlglot). Import error: {e}"
            ) from None
        return OracleService(settings)
    from .postgis import create_service as _pg

    return _pg(settings)


__all__ = ["BackendSpec", "SpatialService", "create_service", "get_spec", "spec_by_name"]
