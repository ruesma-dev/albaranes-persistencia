# infrastructure/database/orm_models.py
from __future__ import annotations

from sqlalchemy import Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class AlbaranDocumentOrm(Base):
    __tablename__ = "albaran_documents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    source_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    source_mime_type: Mapped[str] = mapped_column(String(255), nullable=False)
    source_sha256: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        unique=True,
        index=True,
    )
    source_attachment_filename: Mapped[str | None] = mapped_column(String(255))
    source_attachment_mime_type: Mapped[str | None] = mapped_column(String(255))
    source_attachment_sha256: Mapped[str | None] = mapped_column(String(64))
    page_number: Mapped[int | None] = mapped_column(Integer)
    page_count: Mapped[int | None] = mapped_column(Integer)
    prompt_key: Mapped[str] = mapped_column(String(100), nullable=False)
    schema_name: Mapped[str] = mapped_column(String(100), nullable=False)
    model_name: Mapped[str] = mapped_column(String(100), nullable=False)
    proveedor_nombre: Mapped[str | None] = mapped_column(String(255))
    proveedor_cif: Mapped[str | None] = mapped_column(String(64))
    fecha: Mapped[str | None] = mapped_column(String(32))
    numero_albaran: Mapped[str | None] = mapped_column(String(128))
    forma_pago: Mapped[str | None] = mapped_column(String(128))
    obra_codigo: Mapped[str | None] = mapped_column(String(128))
    obra_nombre: Mapped[str | None] = mapped_column(String(255))
    obra_direccion: Mapped[str | None] = mapped_column(String(255))
    sharepoint_drive_id: Mapped[str | None] = mapped_column(String(255))
    sharepoint_item_id: Mapped[str | None] = mapped_column(String(255))
    sharepoint_relative_path: Mapped[str | None] = mapped_column(String(512))
    sharepoint_web_url: Mapped[str | None] = mapped_column(String(1024))
    sharepoint_share_url: Mapped[str | None] = mapped_column(String(1024))
    ia_input_json: Mapped[str | None] = mapped_column(Text)
    ia_output_json: Mapped[str | None] = mapped_column(Text)
    ia_input_relative_path: Mapped[str | None] = mapped_column(String(1024))
    ia_input_web_url: Mapped[str | None] = mapped_column(String(1024))
    ia_output_relative_path: Mapped[str | None] = mapped_column(String(1024))
    ia_output_web_url: Mapped[str | None] = mapped_column(String(1024))
    email_id: Mapped[str | None] = mapped_column(String(255))
    email_subject: Mapped[str | None] = mapped_column(String(512))
    email_sender: Mapped[str | None] = mapped_column(String(255))
    email_received_datetime: Mapped[str | None] = mapped_column(String(64))
    raw_context_json: Mapped[str | None] = mapped_column(Text)
    raw_extraction_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at_utc: Mapped[str] = mapped_column(String(64), nullable=False)

    lines: Mapped[list["AlbaranLineOrm"]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
    )


class AlbaranLineOrm(Base):
    __tablename__ = "albaran_lines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("albaran_documents.id"),
        nullable=False,
        index=True,
    )
    line_index: Mapped[int] = mapped_column(Integer, nullable=False)
    external_line_id: Mapped[str | None] = mapped_column(String(64))
    cabecera_id: Mapped[str | None] = mapped_column(String(64))
    codigo: Mapped[str | None] = mapped_column(String(64))
    cantidad: Mapped[float | None] = mapped_column(Float)
    concepto: Mapped[str | None] = mapped_column(Text)
    precio: Mapped[float | None] = mapped_column(Float)
    descuento: Mapped[float | None] = mapped_column(Float)
    precio_neto: Mapped[float | None] = mapped_column(Float)
    codigo_imputacion: Mapped[str | None] = mapped_column(String(128))
    confianza_pct: Mapped[float | None] = mapped_column(Float)

    document: Mapped[AlbaranDocumentOrm] = relationship(
        back_populates="lines",
    )
