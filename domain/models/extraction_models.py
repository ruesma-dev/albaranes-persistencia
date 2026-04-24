# domain/models/extraction_models.py
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import Field

from domain.models.contexto_linea import ContextoLinea
from domain.models.schema_base import StrictSchemaModel


class ExtractionMeta(StrictSchemaModel):
    prompt_key: str
    schema_name: str = Field(alias="schema")
    source_filename: str
    source_mime_type: str
    source_sha256: str
    model: str
    processed_at_utc: str
    service: Optional[str] = None
    service_version: Optional[str] = None


class CabeceraAlbaran(StrictSchemaModel):
    proveedor_nombre: Optional[str] = None
    proveedor_cif: Optional[str] = None
    fecha: Optional[str] = None
    numero_albaran: Optional[str] = None
    forma_pago: Optional[str] = None
    obra_codigo: Optional[str] = None
    obra_nombre: Optional[str] = None
    obra_direccion: Optional[str] = None
    id: Optional[str] = None


class LineaAlbaran(StrictSchemaModel):
    id: Optional[str] = None
    cabecera_id: Optional[str] = None
    codigo: Optional[str] = None
    cantidad: Optional[float] = None
    concepto: Optional[str] = None
    # -----------------------------------------------------------------
    # Unidad de medida tal y como aparece en el albarán. Llega del svc2
    # en el envelope. Se persiste en albaran_lines[_merge].unidad_medida.
    # -----------------------------------------------------------------
    unidad_medida: Optional[str] = None
    precio: Optional[float] = None
    descuento: Optional[float] = None
    precio_neto: Optional[float] = None
    codigo_imputacion: Optional[str] = None
    confianza_pct: Optional[float] = Field(default=None, ge=0, le=100)

    # -----------------------------------------------------------------
    # Bloque opcional con info estructural de la línea (familia
    # hormigón / combustible / alquiler_maquinaria / otro). Llega del
    # servicio 2 (OCR) dentro del envelope. Si la línea no es de una
    # familia compleja, el campo llega como None.
    # -----------------------------------------------------------------
    contexto_linea: Optional[ContextoLinea] = None


class DocumentoAlbaran(StrictSchemaModel):
    cabecera: CabeceraAlbaran
    lineas: List[LineaAlbaran]


class ProviderExtractionEnvelope(StrictSchemaModel):
    meta: ExtractionMeta
    data: DocumentoAlbaran
    debug: Dict[str, Any] | None = None


class ExtractionEnvelope(ProviderExtractionEnvelope):
    gemini: ProviderExtractionEnvelope | None = None
    claude: ProviderExtractionEnvelope | None = None
    google_document_ai: ProviderExtractionEnvelope | None = None
    azure_document_intelligence: ProviderExtractionEnvelope | None = None
