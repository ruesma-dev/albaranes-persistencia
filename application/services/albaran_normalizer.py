# application/services/albaran_normalizer.py
from __future__ import annotations

from datetime import datetime
from typing import Optional


class AlbaranNormalizer:
    _DATE_PATTERNS = (
        "%Y-%m-%d",
        "%d/%m/%Y",
        "%d-%m-%Y",
        "%d.%m.%Y",
        "%d/%m/%y",
        "%d-%m-%y",
        "%d.%m.%y",
    )

    def normalize_date(self, value: str | None) -> Optional[str]:
        if not value:
            return None

        raw = value.strip()
        if not raw:
            return None

        for pattern in self._DATE_PATTERNS:
            try:
                parsed = datetime.strptime(raw, pattern)
                return parsed.strftime("%Y-%m-%d")
            except ValueError:
                continue

        return None
