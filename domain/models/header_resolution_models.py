# domain/models/header_resolution_models.py
"""Modelos del HeaderResolverService (resolucion determinista de
cabecera: obra_codigo / proveedor_cif por coincidencia de texto)."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MergeHeaderForResolution:
    """Snapshot de los campos de cabecera del merge que intervienen en
    la resolucion determinista. Lo lee el repositorio."""

    obra_codigo: str | None
    obra_nombre: str | None
    obra_direccion: str | None
    proveedor_cif: str | None
    proveedor_nombre: str | None
