# domain/models/contrato_models.py
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ContratoLineFromSigrid:
    """Línea de detalle (``ctrpro``) de un contrato del ERP.

    Asociada a su cabecera por ``codigo_contrato`` (único dentro de un
    resultset por (cif, obra) — lo validamos con diagnose_sigrid_contrato).

    Los valores numéricos vienen como DECIMAL de SQL Server; el adaptador
    Sigrid los convierte a ``float`` en Python para consistencia con el
    resto del dominio. Las cantidades de tracking (``cantidad_servida``,
    ``cantidad_facturada``) pueden ser None al inicio del ciclo del
    contrato, antes de que se hayan emitido albaranes/facturas.
    """

    codigo_contrato: str
    linea: int | None
    numero_linea: int | None
    codigo_producto: str | None
    codigo_alternativo: str | None
    unidad_medida: str | None
    descripcion_linea: str | None
    uds: float | None
    cantidad_servida: float | None
    cantidad_facturada: float | None
    pendiente_servir: float | None
    precio_unitario: float | None
    precio_bruto: float | None
    descuentos: float | None
    importe_linea: float | None
    cuota_iva: float | None
    doc_origen: str | None


@dataclass(frozen=True)
class ContratoEnrichmentResult:
    """Cabecera de contrato + líneas asociadas tal como se persisten.

    El adaptador Sigrid devuelve una lista de estos objetos tras agrupar
    el resultset de la query ampliada (una sola llamada HTTP). El
    campo ``lines`` nunca es None — lista vacía si el contrato no tiene
    líneas de detalle en el ERP.
    """

    codigo_contrato: str
    nombre_contrato: str | None
    fecha_alta_contrato: int | None
    fecha_contrato: int | None
    vigencia_desde: int | None
    vigencia_hasta: int | None
    importe_total: float | None
    cif_proveedor: str | None
    nombre_proveedor: str | None
    codigo_obra: str | None
    nombre_obra: str | None
    lines: list[ContratoLineFromSigrid] = field(default_factory=list)
