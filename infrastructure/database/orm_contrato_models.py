# infrastructure/database/orm_contrato_models.py
from __future__ import annotations

from sqlalchemy import (
    Column,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from infrastructure.database.orm_models import Base


class AlbaranContratoMergeOrm(Base):
    """Contratos (cabecera) asociados a un albarán por (CIF, obra).

    ``importe_total`` = ``ctr.totbas`` (sin IVA).
    ``gra_rep_ide`` = id del PDF del contrato en ``ruesma_rep.gra``.
    ``pdf_sharepoint_relative_path`` / ``pdf_sharepoint_web_url`` =
    ubicación del PDF ya subido a SharePoint (rellenados tras la
    descarga+subida automática en el enrichment).

    ``sigrid_ide`` = ``ctr.ide`` en Sigrid (INDICE PRIMARIO, entero).
    Identifica el contrato en el ERP de forma estable. Se usa como
    clave de UPSERT: cuando llega un albarán nuevo cuyo enriquecimiento
    devuelve un contrato con el mismo ``sigrid_ide`` que ya existe en
    BBDD, se ACTUALIZA en lugar de duplicar. Es UNIQUE para garantizar
    que la BBDD nunca tenga dos filas describiendo el mismo contrato
    del ERP.

    NOTA — El UNIQUE de ``sigrid_ide`` NO se declara aquí con
    ``unique=True``. Se crea desde ``schema_contribution.py`` como
    ``CREATE UNIQUE INDEX ... WHERE sigrid_ide IS NOT NULL``. Razón:
    queremos permitir múltiples filas legacy con ``sigrid_ide = NULL``
    (filas anteriores al refactor que aún no han sido re-enriquecidas
    desde Sigrid). Un UNIQUE clásico aceptaría también múltiples NULLs
    en PostgreSQL, pero el índice parcial es más explícito sobre la
    intención.

    ``document_id`` ya NO es la clave de identidad del contrato (lo es
    ``sigrid_ide``). Se mantiene por trazabilidad: indica qué albarán
    tocó este contrato más recientemente. Es nullable y la FK ahora es
    ``ON DELETE SET NULL`` para permitir que un albarán se borre sin
    arrastrar contratos que pueden estar siendo referenciados por
    otros albaranes.
    """

    __tablename__ = "albaran_contratos_merge"

    id = Column(Integer, primary_key=True, autoincrement=True)
    document_id = Column(
        String(64),
        ForeignKey("albaran_documents_merge.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    sigrid_ide = Column(Integer, nullable=True, index=True)
    codigo_contrato = Column(String(64), nullable=False)
    nombre_contrato = Column(Text, nullable=True)
    fecha_alta_contrato = Column(Integer, nullable=True)
    fecha_contrato = Column(Integer, nullable=True)
    vigencia_desde = Column(Integer, nullable=True)
    vigencia_hasta = Column(Integer, nullable=True)
    importe_total = Column(Float, nullable=True)  # ctr.totbas (sin IVA)
    cif_proveedor = Column(String(32), nullable=True)
    nombre_proveedor = Column(Text, nullable=True)
    codigo_obra = Column(String(32), nullable=True)
    nombre_obra = Column(Text, nullable=True)
    gra_rep_ide = Column(Integer, nullable=True)
    pdf_sharepoint_relative_path = Column(String(1024), nullable=True)
    pdf_sharepoint_web_url = Column(String(1024), nullable=True)
    fetched_at_utc = Column(String(64), nullable=False)

    lines = relationship(
        "AlbaranContratoLineMergeOrm",
        back_populates="contrato",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        Index(
            "ix_albaran_contratos_merge_doc_codigo",
            "document_id",
            "codigo_contrato",
        ),
    )


class AlbaranContratoLineMergeOrm(Base):
    """Líneas de detalle (``ctrpro``) de un contrato de cabecera.

    Incluye partida (``obrparpar.cod`` / ``obrparpar.res``).

    ``sigrid_ide`` = ``ctrpro.ide`` en Sigrid (INDICE PRIMARIO, entero).
    Identifica la línea en el ERP de forma estable. Se usa como clave
    de UPSERT: una misma línea (mismo ``ctrpro.ide``) se actualiza en
    lugar de duplicar. UNIQUE parcial (WHERE sigrid_ide IS NOT NULL)
    para permitir múltiples NULLs históricos — ver comentario en
    ``AlbaranContratoMergeOrm.sigrid_ide``.
    """

    __tablename__ = "albaran_contrato_lines_merge"

    id = Column(Integer, primary_key=True, autoincrement=True)
    contrato_id = Column(
        Integer,
        ForeignKey("albaran_contratos_merge.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    sigrid_ide = Column(Integer, nullable=True, index=True)
    codigo_contrato = Column(String(64), nullable=False)
    linea = Column(Integer, nullable=True)
    numero_linea = Column(Integer, nullable=True)
    codigo_producto = Column(String(64), nullable=True)
    codigo_alternativo = Column(String(64), nullable=True)
    unidad_medida = Column(String(32), nullable=True)
    descripcion_linea = Column(Text, nullable=True)
    uds = Column(Float, nullable=True)
    cantidad_servida = Column(Float, nullable=True)
    cantidad_facturada = Column(Float, nullable=True)
    pendiente_servir = Column(Float, nullable=True)
    precio_unitario = Column(Float, nullable=True)
    precio_bruto = Column(Float, nullable=True)
    descuentos = Column(Float, nullable=True)
    importe_linea = Column(Float, nullable=True)
    cuota_iva = Column(Float, nullable=True)
    doc_origen = Column(String(64), nullable=True)
    codigo_partida = Column(String(64), nullable=True)
    descripcion_partida = Column(Text, nullable=True)
    fetched_at_utc = Column(String(64), nullable=False)

    contrato = relationship(
        "AlbaranContratoMergeOrm",
        back_populates="lines",
    )

    __table_args__ = (
        Index(
            "ix_albaran_contrato_lines_merge_codigo",
            "codigo_contrato",
        ),
        Index(
            "ix_albaran_contrato_lines_merge_partida",
            "codigo_partida",
        ),
    )
