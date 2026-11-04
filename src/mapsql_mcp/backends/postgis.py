"""PostGIS backend: the original implementation lives in ``service.py`` / ``db.py`` / ``layers.py`` / ``guard.py``."""

from __future__ import annotations

from ..config import Settings
from .base import BackendSpec

SPEC = BackendSpec(
    name="postgis",
    label="PostGIS",
    sql_dialect="PostgreSQL/PostGIS",
    server_info_hint="PostgreSQL/PostGIS versions",
    run_sql_hint="single SELECT, PostgreSQL/PostGIS syntax",
)


def create_service(settings: Settings):
    from ..service import GeoService

    return GeoService(settings)
