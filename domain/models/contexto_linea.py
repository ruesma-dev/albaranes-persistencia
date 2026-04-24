# domain/models/contexto_linea.py
"""Modelo compartido del bloque ``contexto_linea``.

Representa la información estructural de una línea de albarán cuando
pertenece a una familia compleja (hormigón, combustible, alquiler de
maquinaria). Este fichero se copia IDÉNTICO en los servicios 2, 3, 5
y 6 que manipulan el envelope.

Campos (ver prompts V2 para semántica completa):
  - tipo_familia: 'hormigon' | 'combustible' | 'alquiler_maquinaria'
                  | 'otro' | null
  - rol_linea: 'base' | 'extra_tiempo' | 'transporte' | 'recargo_horario'
               | 'desplazamiento' | 'operario' | 'otro' | null
  - descripcion_extendida: string con la descripción técnica completa
    (incluye modificadores del producto base).
  - notas_tiempo: string libre con info temporal del albarán.
  - ref_linea_base: int con el ``line_index`` de la línea base asociada
    cuando esta línea es complementaria.

El bloque es OPCIONAL. Usamos ``extra='ignore'`` para que el LLM pueda
devolver campos adicionales sin romper la validación.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

TipoFamilia = Literal[
    "hormigon",
    "combustible",
    "alquiler_maquinaria",
    "otro",
]

RolLinea = Literal[
    "base",
    "extra_tiempo",
    "transporte",
    "recargo_horario",
    "desplazamiento",
    "operario",
    "otro",
]


class ContextoLinea(BaseModel):
    """Bloque opcional con info estructural de la línea."""

    model_config = ConfigDict(extra="ignore")

    tipo_familia: Optional[TipoFamilia] = Field(default=None)
    rol_linea: Optional[RolLinea] = Field(default=None)
    descripcion_extendida: Optional[str] = Field(default=None)
    notas_tiempo: Optional[str] = Field(default=None)
    ref_linea_base: Optional[int] = Field(default=None)
