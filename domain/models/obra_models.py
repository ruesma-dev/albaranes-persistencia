# domain/models/obra_models.py
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ObraEnrichmentResult:
    """Datos canónicos de una obra recuperados desde la BBDD on-prem (Sigrid).

    Mapea las columnas del SELECT contra ``obr``/``con``/``auxmun``/``auxpro``
    (ver ``SigridApiObraClient``). Todos los campos excepto ``codigo_obra``
    pueden ser ``None`` (la BBDD real devuelve ``NULL`` en direcciones
    incompletas).
    """

    codigo_obra: str
    nombre_obra: str | None
    direccion_linea1: str | None
    direccion_linea2: str | None
    codigo_postal: str | None
    municipio: str | None
    provincia: str | None

    @property
    def direccion_completa(self) -> str | None:
        """Compone una dirección legible saltando partes vacías.

        Formato: ``dir1[, dir2][, CP municipio][ (provincia)]``.

        Ejemplos:
          - dir1='Avda. Democracia nº 9', cp='28031', municipio='MADRID',
            provincia='MADRID'
            → 'Avda. Democracia nº 9, 28031 MADRID (MADRID)'
          - solo provincia='Teruel' → '(Teruel)'
          - todo vacío → None
        """
        parts: list[str] = []
        if self.direccion_linea1 and self.direccion_linea1.strip():
            parts.append(self.direccion_linea1.strip())
        if self.direccion_linea2 and self.direccion_linea2.strip():
            parts.append(self.direccion_linea2.strip())

        locality_tokens: list[str] = []
        if self.codigo_postal and self.codigo_postal.strip():
            locality_tokens.append(self.codigo_postal.strip())
        if self.municipio and self.municipio.strip():
            locality_tokens.append(self.municipio.strip())
        if locality_tokens:
            parts.append(" ".join(locality_tokens))

        direccion = ", ".join(parts)
        if self.provincia and self.provincia.strip():
            provincia = self.provincia.strip()
            direccion = f"{direccion} ({provincia})" if direccion else f"({provincia})"
        return direccion or None

    @property
    def richness_score(self) -> int:
        """Número de campos informativos no vacíos.

        Usado para rankear cuando la BBDD devuelve varias filas para el
        mismo ``con.cod`` (caso real: dos registros del 0695, uno con la
        dirección completa y otro con casi todo vacío). Elegimos la fila
        con mayor ``richness_score`` para el enriquecimiento.
        """
        score = 0
        for value in (
            self.nombre_obra,
            self.direccion_linea1,
            self.direccion_linea2,
            self.codigo_postal,
            self.municipio,
            self.provincia,
        ):
            if value and str(value).strip():
                score += 1
        return score
