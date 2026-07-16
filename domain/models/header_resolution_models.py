# domain/models/header_resolution_models.py
"""Modelos del HeaderResolverService (resolucion determinista de
cabecera: obra_codigo / proveedor_cif por coincidencia de texto y, desde
jul 2026, por familia de producto + proveedores con contrato en la obra)."""
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


@dataclass(frozen=True)
class ProveedorObraResumen:
    """Resumen de UN proveedor con contrato en una obra: su identidad
    canonica y el TEXTO agregado de sus contratos en esa obra (nombres de
    contrato + descripciones de linea + codigos de producto), para que el
    resolver detecte la familia (hormigon, residuos, mortero...) con las
    mismas reglas que el selector de contratos.

    Lo construye ``SigridApiContratoClient.fetch_contratos_resumen_por_obra``.
    """

    cif: str
    nombre: str | None
    codigos_contratos: tuple[str, ...]
    texto: str
