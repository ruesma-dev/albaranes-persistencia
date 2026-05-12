# infrastructure/database/sqlalchemy_contrato_cache_repository.py
"""Implementación SQLAlchemy del puerto ``ContratoCachePort``.

Persiste contratos en las tablas ``contratos_cache`` y
``contrato_cache_lines`` (modelos ORM definidos en
``orm_contrato_cache_models.py``).

Notas de implementación:

  * **Lookup por vigencia**: las columnas
    ``vigencia_desde`` y ``vigencia_hasta`` son ``Integer`` con
    formato ``YYYYMMDD`` (heredado de Sigrid). El servicio de
    enrichment convierte la ``fecha`` del albarán
    (``"2026-03-11"``) al mismo formato (``20260311``) antes de
    invocar ``find_active_contrato``.

  * **Vigencias NULL**: si una fila cacheada tiene NULL en
    ``vigencia_desde`` o ``vigencia_hasta``, no podemos garantizar
    que cubra una fecha → la descartamos (es seguro: el siguiente
    paso será una llamada a Sigrid).

  * **Múltiples candidatos**: si hay varias filas con vigencias que
    cubren la fecha, elegimos la de mayor ``fecha_alta_contrato``
    (versión más reciente).

  * **Upsert**: usamos ``INSERT ... ON CONFLICT DO UPDATE`` de
    PostgreSQL contra la UNIQUE cuádruple. Para las líneas hacemos
    delete + insert (más simple que upsert por línea, y aquí no hay
    contención de concurrencia porque las líneas siempre se reescriben
    en bloque).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from domain.models.contrato_models import (
    ContratoEnrichmentResult,
    ContratoLineFromSigrid,
)
from infrastructure.database.orm_contrato_cache_models import (
    ContratoCacheLineOrm,
    ContratoCacheOrm,
)
from infrastructure.database.session_factory import SessionFactory

logger = logging.getLogger(__name__)

_LOG_PREFIX = "[contrato-cache][repo]"


class SqlAlchemyContratoCacheRepository:
    """Implementación de ``ContratoCachePort`` sobre PostgreSQL."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory
        logger.info("%s Instanciado.", _LOG_PREFIX)

    # ------------------------------------------------------------------ #
    # Lectura
    # ------------------------------------------------------------------ #
    def find_active_contrato(
        self,
        *,
        codigo_obra: str,
        cif_proveedor: str,
        fecha_albaran_yyyymmdd: int,
    ) -> ContratoEnrichmentResult | None:
        with self._session_factory.create_session() as session:
            stmt = (
                select(ContratoCacheOrm)
                .where(
                    and_(
                        ContratoCacheOrm.codigo_obra == codigo_obra,
                        ContratoCacheOrm.cif_proveedor == cif_proveedor,
                        ContratoCacheOrm.vigencia_desde.is_not(None),
                        ContratoCacheOrm.vigencia_hasta.is_not(None),
                        ContratoCacheOrm.vigencia_desde <= fecha_albaran_yyyymmdd,
                        ContratoCacheOrm.vigencia_hasta >= fecha_albaran_yyyymmdd,
                    )
                )
                # Más reciente primero por si hay versiones; desempate
                # estable por id.
                .order_by(
                    ContratoCacheOrm.fecha_alta_contrato.desc().nulls_last(),
                    ContratoCacheOrm.id.desc(),
                )
            )

            candidates = list(session.scalars(stmt))
            logger.info(
                "%s find_active_contrato obra=%s cif=%s fecha=%s -> %s candidato(s).",
                _LOG_PREFIX,
                codigo_obra,
                cif_proveedor,
                fecha_albaran_yyyymmdd,
                len(candidates),
            )

            if not candidates:
                return None

            chosen = candidates[0]
            if len(candidates) > 1:
                logger.info(
                    "%s   varios candidatos; elegido id=%s codigo=%s "
                    "fecha_alta=%s vigencia=[%s, %s].",
                    _LOG_PREFIX,
                    chosen.id,
                    chosen.codigo_contrato,
                    chosen.fecha_alta_contrato,
                    chosen.vigencia_desde,
                    chosen.vigencia_hasta,
                )

            return self._orm_to_dto(chosen)

    @staticmethod
    def _orm_to_dto(
        cabecera: ContratoCacheOrm,
    ) -> ContratoEnrichmentResult:
        """Convierte cabecera + líneas en ``ContratoEnrichmentResult``."""
        lines = [
            ContratoLineFromSigrid(
                codigo_contrato=line.codigo_contrato,
                linea=line.linea,
                numero_linea=line.numero_linea,
                codigo_producto=line.codigo_producto,
                codigo_alternativo=line.codigo_alternativo,
                unidad_medida=line.unidad_medida,
                descripcion_linea=line.descripcion_linea,
                uds=line.uds,
                cantidad_servida=line.cantidad_servida,
                cantidad_facturada=line.cantidad_facturada,
                pendiente_servir=line.pendiente_servir,
                precio_unitario=line.precio_unitario,
                precio_bruto=line.precio_bruto,
                descuentos=line.descuentos,
                importe_linea=line.importe_linea,
                cuota_iva=line.cuota_iva,
                doc_origen=line.doc_origen,
                codigo_partida=line.codigo_partida,
                descripcion_partida=line.descripcion_partida,
                sigrid_ide=line.sigrid_ide,
            )
            for line in cabecera.lines
        ]

        return ContratoEnrichmentResult(
            codigo_contrato=cabecera.codigo_contrato,
            nombre_contrato=cabecera.nombre_contrato,
            fecha_alta_contrato=cabecera.fecha_alta_contrato,
            fecha_contrato=cabecera.fecha_contrato,
            vigencia_desde=cabecera.vigencia_desde,
            vigencia_hasta=cabecera.vigencia_hasta,
            importe_total=cabecera.importe_total,
            cif_proveedor=cabecera.cif_proveedor,
            nombre_proveedor=cabecera.nombre_proveedor,
            codigo_obra=cabecera.codigo_obra,
            nombre_obra=cabecera.nombre_obra,
            gra_rep_ide=cabecera.gra_rep_ide,
            pdf_sharepoint_relative_path=cabecera.pdf_sharepoint_relative_path,
            pdf_sharepoint_web_url=cabecera.pdf_sharepoint_web_url,
            sigrid_ide=cabecera.sigrid_ide,
            lines=lines,
        )

    # ------------------------------------------------------------------ #
    # Escritura
    # ------------------------------------------------------------------ #
    def upsert_contratos(
        self,
        *,
        contratos: list[ContratoEnrichmentResult],
    ) -> int:
        """Persiste o actualiza contratos en la caché.

        Cada contrato es identificado por la UNIQUE cuádruple
        ``(codigo_obra, cif_proveedor, codigo_contrato, fecha_alta_contrato)``.
        Si ya existe, refresca cabecera + líneas (delete + insert).
        Si no, inserta.

        Filas con datos clave faltantes (``codigo_obra`` o
        ``cif_proveedor`` vacíos) se omiten silenciosamente.
        """
        if not contratos:
            return 0

        now = datetime.now(timezone.utc).isoformat()
        written = 0

        with self._session_factory.create_session() as session:
            for contrato in contratos:
                if not contrato.codigo_obra or not contrato.cif_proveedor:
                    logger.warning(
                        "%s Omitida fila sin obra/cif: codigo=%s",
                        _LOG_PREFIX,
                        contrato.codigo_contrato,
                    )
                    continue

                cabecera_id = self._upsert_cabecera(
                    session=session,
                    contrato=contrato,
                    now=now,
                )
                if cabecera_id is None:
                    continue
                self._replace_lines(
                    session=session,
                    cabecera_id=cabecera_id,
                    codigo_contrato=contrato.codigo_contrato,
                    lines=contrato.lines or [],
                    now=now,
                )
                written += 1

            session.commit()

        logger.info(
            "%s upsert_contratos escritos=%s/%s",
            _LOG_PREFIX,
            written,
            len(contratos),
        )
        return written

    def _upsert_cabecera(
        self,
        *,
        session: Any,
        contrato: ContratoEnrichmentResult,
        now: str,
    ) -> int | None:
        """INSERT ... ON CONFLICT DO UPDATE de la cabecera.

        Devuelve el id de la fila resultante (insertada o actualizada),
        o ``None`` si la operación no devuelve fila (no debería pasar).
        """
        # PostgreSQL exige que las columnas de la UNIQUE estén entre los
        # valores no nulos para el ON CONFLICT. fecha_alta_contrato puede
        # ser NULL en BBDD; en ese caso PostgreSQL no considera duplicada
        # la fila (NULL != NULL en UNIQUE), y haríamos INSERT múltiple.
        # Esto es aceptable: si no hay fecha_alta no podemos discriminar
        # versiones, y dejar varias filas equivalentes no rompe nada.
        values = {
            "codigo_obra": contrato.codigo_obra,
            "cif_proveedor": contrato.cif_proveedor,
            "codigo_contrato": contrato.codigo_contrato,
            "fecha_alta_contrato": contrato.fecha_alta_contrato,
            "sigrid_ide": contrato.sigrid_ide,
            "nombre_contrato": contrato.nombre_contrato,
            "fecha_contrato": contrato.fecha_contrato,
            "vigencia_desde": contrato.vigencia_desde,
            "vigencia_hasta": contrato.vigencia_hasta,
            "importe_total": contrato.importe_total,
            "nombre_proveedor": contrato.nombre_proveedor,
            "nombre_obra": contrato.nombre_obra,
            "gra_rep_ide": contrato.gra_rep_ide,
            "pdf_sharepoint_relative_path": contrato.pdf_sharepoint_relative_path,
            "pdf_sharepoint_web_url": contrato.pdf_sharepoint_web_url,
            "fetched_at_utc": now,
        }

        stmt = pg_insert(ContratoCacheOrm).values(**values)
        # Campos a refrescar en conflicto (todos menos las columnas
        # clave: obra, cif, codigo, fecha_alta).
        update_columns = {
            col: getattr(stmt.excluded, col)
            for col in (
                "sigrid_ide",
                "nombre_contrato",
                "fecha_contrato",
                "vigencia_desde",
                "vigencia_hasta",
                "importe_total",
                "nombre_proveedor",
                "nombre_obra",
                "gra_rep_ide",
                "pdf_sharepoint_relative_path",
                "pdf_sharepoint_web_url",
                "fetched_at_utc",
            )
        }
        stmt = stmt.on_conflict_do_update(
            constraint="uq_contratos_cache_quad",
            set_=update_columns,
        ).returning(ContratoCacheOrm.id)

        try:
            cabecera_id = session.execute(stmt).scalar_one()
        except Exception:
            logger.exception(
                "%s FALLO upsert cabecera codigo=%s obra=%s cif=%s",
                _LOG_PREFIX,
                contrato.codigo_contrato,
                contrato.codigo_obra,
                contrato.cif_proveedor,
            )
            return None

        return int(cabecera_id) if cabecera_id is not None else None

    @staticmethod
    def _replace_lines(
        *,
        session: Any,
        cabecera_id: int,
        codigo_contrato: str,
        lines: list[ContratoLineFromSigrid],
        now: str,
    ) -> None:
        """Borra todas las líneas de la cabecera y reinserta."""
        session.execute(
            delete(ContratoCacheLineOrm).where(
                ContratoCacheLineOrm.contrato_cache_id == cabecera_id
            )
        )

        for line in lines:
            session.add(
                ContratoCacheLineOrm(
                    contrato_cache_id=cabecera_id,
                    sigrid_ide=line.sigrid_ide,
                    codigo_contrato=codigo_contrato,
                    linea=line.linea,
                    numero_linea=line.numero_linea,
                    codigo_producto=line.codigo_producto,
                    codigo_alternativo=line.codigo_alternativo,
                    unidad_medida=line.unidad_medida,
                    descripcion_linea=line.descripcion_linea,
                    uds=line.uds,
                    cantidad_servida=line.cantidad_servida,
                    cantidad_facturada=line.cantidad_facturada,
                    pendiente_servir=line.pendiente_servir,
                    precio_unitario=line.precio_unitario,
                    precio_bruto=line.precio_bruto,
                    descuentos=line.descuentos,
                    importe_linea=line.importe_linea,
                    cuota_iva=line.cuota_iva,
                    doc_origen=line.doc_origen,
                    codigo_partida=line.codigo_partida,
                    descripcion_partida=line.descripcion_partida,
                    fetched_at_utc=now,
                )
            )
