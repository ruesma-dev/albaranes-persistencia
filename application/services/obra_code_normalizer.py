# application/services/obra_code_normalizer.py
from __future__ import annotations


def normalize_obra_code(raw_value: str | None) -> str | None:
    """Normaliza un código de obra al formato canónico de Ruesma.

    Reglas:
      - 4 dígitos, el primero siempre ``0``.
      - Si viene con 3 dígitos, se antepone ``0`` (``695`` → ``0695``).
      - Cualquier otro patrón devuelve ``None`` y el llamador omite la
        consulta a Sigrid.

    Ejemplos:
        >>> normalize_obra_code("0695")
        '0695'
        >>> normalize_obra_code("695")
        '0695'
        >>> normalize_obra_code("  695  ")
        '0695'
        >>> normalize_obra_code("1234")   # no empieza por 0
        >>> normalize_obra_code("12345")  # demasiado largo
        >>> normalize_obra_code("abc")
        >>> normalize_obra_code(None)
    """
    if raw_value is None:
        return None
    cleaned = str(raw_value).strip()
    if not cleaned or not cleaned.isdigit():
        return None
    if len(cleaned) == 3:
        return "0" + cleaned
    if len(cleaned) == 4 and cleaned.startswith("0"):
        return cleaned
    return None
