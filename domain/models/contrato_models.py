# domain/models/contrato_models.py
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ContratoEnrichmentResult:
    """Datos canónicos de un contrato recuperados desde Sigrid (on-prem).

    Mapea 1:1 las columnas de la query del documento de diseño:
      SELECT con_ctr.cod, con_ctr.res, con_ctr.fec,
             ctr.fecdoc, ctr.fecvig1, ctr.fecvig2, ctr.tot,
             ctr.entcif, ctr.entres,
             con_obr.cod, con_obr.res
      FROM ctr JOIN con AS con_ctr ... JOIN con AS con_obr ... JOIN prv ...
      WHERE prv.cif = ? AND con_obr.cod = ?

    Tipos:
      - Las fechas en Sigrid vienen como INT en formato YYYYMMDD
        (ej. 20241122). Se guardan tal cual; el formateo a DD/MM/YYYY
        se hace en la capa de presentación.
      - Las fechas de vigencia pueden venir como 0 si no hay vigencia
        establecida.
      - ``importe_total`` viene como float.
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
