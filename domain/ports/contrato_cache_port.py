# domain/ports/contrato_cache_port.py
"""Puerto para la caché compartida de contratos.

La caché se implementa fuera del dominio (en
``infrastructure/database/sqlalchemy_contrato_cache_repository.py``),
y se inyecta en ``ContratoEnrichmentService`` para evitar llamadas
repetidas a Sigrid cuando ya tenemos un contrato vigente que cubre
la fecha del albarán.

La idea conceptual es: la caché es **agnóstica al document_id del
albarán**. Solo sabe de contratos por (obra, proveedor, código,
fecha_alta). El servicio de enrichment se encarga de copiar el
contrato cacheado a ``albaran_contratos_merge`` con el
``document_id`` que toque.
"""
from __future__ import annotations

from typing import Protocol

from domain.models.contrato_models import ContratoEnrichmentResult


class ContratoCachePort(Protocol):
    """Interfaz mínima de la caché de contratos.

    Implementaciones concretas:
      - ``SqlAlchemyContratoCacheRepository``: persiste en
        PostgreSQL (tablas ``contratos_cache`` y ``contrato_cache_lines``).
    """

    def find_active_contrato(
        self,
        *,
        codigo_obra: str,
        cif_proveedor: str,
        fecha_albaran_yyyymmdd: int,
    ) -> ContratoEnrichmentResult | None:
        """Busca un contrato cacheado vigente para la fecha del albarán.

        Filtros:
          - codigo_obra y cif_proveedor exactos.
          - vigencia_desde ≤ fecha_albaran ≤ vigencia_hasta. Si las
            vigencias están a NULL, el candidato se descarta (no
            podemos garantizar que cubra la fecha).

        Si hay varios candidatos, devuelve el de mayor
        ``fecha_alta_contrato`` (versión más reciente).

        Devuelve ``None`` si no hay match.
        """
        ...

    def get_pdf_paths_for_codigos(
        self,
        *,
        codigo_obra: str,
        cif_proveedor: str,
        codigos: list[str],
    ) -> dict[str, tuple[int | None, str | None, str | None]]:
        """Paths de PDF ya subidos a SharePoint para esos contratos.

        Busca en la caché global por (obra, cif) las filas cuyos
        ``codigo_contrato`` estén en ``codigos`` y tengan
        ``pdf_sharepoint_relative_path`` no nulo. Si hay varias
        versiones del mismo código, gana la de mayor
        ``fecha_alta_contrato``.

        Devuelve ``{codigo_contrato: (gra_rep_ide, relative_path,
        web_url)}`` solo con los códigos encontrados. Permite REUTILIZAR
        el PDF de un contrato ya descargado en el pasado (incluso por
        otro albarán o tras cambiar de contrato y volver) sin repetir
        la descarga de Sigrid ni la subida a SharePoint.
        """
        ...

    def upsert_contratos(
        self,
        *,
        contratos: list[ContratoEnrichmentResult],
    ) -> int:
        """Persiste/actualiza una lista de contratos en la caché.

        Sobre la clave UNIQUE
        ``(codigo_obra, cif_proveedor, codigo_contrato, fecha_alta_contrato)``:
          - Si la fila no existe → INSERT.
          - Si existe → UPDATE (refresca todos los campos + líneas).

        Devuelve el número de cabeceras escritas (insertadas o
        actualizadas).
        """
        ...
