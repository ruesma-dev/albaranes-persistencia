# infrastructure/database/orm_contrato_models.py
from __future__ import annotations

from sqlalchemy import Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

# Reusamos la MISMA instancia de ``Base`` del módulo principal para que
# ``Base.metadata.create_all(engine)`` descubra también esta tabla.
# Importante: si cambias el Base a uno nuevo, SQLAlchemy NO registra la
# tabla y no se crea.
from infrastructure.database.orm_models import Base


class AlbaranContratoMergeOrm(Base):
    """Contratos devueltos por Sigrid para una fila merge.

    Relación 1:N:
        albaran_documents_merge 1 ─── N albaran_contratos_merge

    La referencia al documento va por FK con ON DELETE CASCADE: si se
    elimina el merge, sus contratos se van con él.

    Se guardan TODAS las columnas que devuelve la query (aunque la UI
    solo muestre 4). Así tenemos trazabilidad completa para auditoría.
    """

    __tablename__ = "albaran_contratos_merge"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            "albaran_documents_merge.id",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
    )

    # Columnas clonadas de la query de Sigrid (11 campos).
    codigo_contrato: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    nombre_contrato: Mapped[str | None] = mapped_column(Text)
    fecha_alta_contrato: Mapped[int | None] = mapped_column(Integer)  # YYYYMMDD
    fecha_contrato: Mapped[int | None] = mapped_column(Integer)  # YYYYMMDD
    vigencia_desde: Mapped[int | None] = mapped_column(Integer)  # YYYYMMDD o 0
    vigencia_hasta: Mapped[int | None] = mapped_column(Integer)  # YYYYMMDD o 0
    importe_total: Mapped[float | None] = mapped_column(Float)
    cif_proveedor: Mapped[str | None] = mapped_column(String(32))
    nombre_proveedor: Mapped[str | None] = mapped_column(String(255))
    codigo_obra: Mapped[str | None] = mapped_column(String(32))
    nombre_obra: Mapped[str | None] = mapped_column(String(255))

    # Auditoría: cuándo se recuperó este contrato de Sigrid.
    fetched_at_utc: Mapped[str] = mapped_column(String(64), nullable=False)
