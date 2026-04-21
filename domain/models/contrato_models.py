# domain/models/contrato_models.py
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ContratoLineFromSigrid:
    """Línea de detalle (``ctrpro``) de un contrato del ERP.

    Incluye los datos de la partida a la que se imputa la línea
    (``obrparpar``): útil para agrupar líneas por capítulo de obra.

    Los valores numéricos vienen como DECIMAL de SQL Server; el adaptador
    Sigrid los convierte a ``float`` en Python.
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
    codigo_partida: str | None
    descripcion_partida: str | None


@dataclass(frozen=True)
class ContratoEnrichmentResult:
    """Cabecera de contrato + líneas + referencia al PDF.

    ``importe_total`` viene de ``ctr.totbas`` (importe SIN IVA).

    ``gra_rep_ide`` es el id del documento PDF del contrato en
    ``ruesma_rep.gra``. Es el PDF principal del contrato; si hay
    varios PDFs vinculados se guarda el primero en orden de ``rcg.pos``.
    ``None`` si el contrato no tiene PDF vinculado en el ERP.

    Los campos ``pdf_sharepoint_*`` se rellenan TRAS la descarga/subida
    del PDF a SharePoint. Son transitorios a nivel del DTO:
      - En la primera ejecución van ``None`` (se rellenan después).
      - Cuando se reutiliza un PDF ya subido (mismo gra_rep_ide) se
        pueden inyectar directamente sin descarga.
    """

    codigo_contrato: str
    nombre_contrato: str | None
    fecha_alta_contrato: int | None
    fecha_contrato: int | None
    vigencia_desde: int | None
    vigencia_hasta: int | None
    importe_total: float | None  # ctr.totbas (sin IVA)
    cif_proveedor: str | None
    nombre_proveedor: str | None
    codigo_obra: str | None
    nombre_obra: str | None
    gra_rep_ide: int | None
    pdf_sharepoint_relative_path: str | None = None
    pdf_sharepoint_web_url: str | None = None
    lines: list[ContratoLineFromSigrid] = field(default_factory=list)
