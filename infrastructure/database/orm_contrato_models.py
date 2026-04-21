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
)
from sqlalchemy.orm import relationship

from infrastructure.database.orm_models import Base


class AlbaranContratoMergeOrm(Base):
    """Contratos (cabecera) asociados a un albarán por (CIF, obra).

    Relación N:1 con ``albaran_documents_merge``: un documento puede
    tener N contratos si el ERP devuelve múltiples para la combinación.
    La selección concreta se guarda en
    ``albaran_documents_merge.selected_contrato_codigo``.

    ``importe_total`` almacena ``ctr.totbas`` (importe SIN IVA del ERP).
    ``gra_rep_ide`` apunta al PDF del contrato en ``ruesma_rep.gra``;
    usable luego para descargar el PDF vía ``/api/documents/read``.
    """

    __tablename__ = "albaran_contratos_merge"

    id = Column(Integer, primary_key=True, autoincrement=True)
    document_id = Column(
        String(64),
        ForeignKey("albaran_documents_merge.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
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
    fetched_at_utc = Column(String(64), nullable=False)

    # cascade + passive_deletes: al borrar un contrato, las líneas caen
    # a nivel BBDD por ON DELETE CASCADE. SQLAlchemy no emite DELETEs
    # explícitos (passive_deletes=True).
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

    Incluye la partida (``obrparpar.cod`` / ``obrparpar.res``) a la que
    se imputa la línea en el desglose de obra.

    Cascada: ``ondelete="CASCADE"`` a nivel BBDD.
    """

    __tablename__ = "albaran_contrato_lines_merge"

    id = Column(Integer, primary_key=True, autoincrement=True)
    contrato_id = Column(
        Integer,
        ForeignKey("albaran_contratos_merge.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
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
