#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Diagnóstico SIGRID — documentos PDF de un contrato de compra.

Busca documentos asociados a un contrato por obra + proveedor y permite
comprobar un PDF partiendo directamente de su nombre/código en gra.

Vías de búsqueda de documentos de contrato:

  VÍA 1 — RCG + gra:
    ruesma.rcg.con = ctr.ide -> rcg.gra -> ruesma_rep.dbo.gra.ide

  VÍA 2 — DOG.ctride:
    ruesma.dog.ctride = ctr.ide

  VÍA 3 — CONDOG:
    ruesma.condog.conide = ctr.ide -> condog.dogide -> dog.ide

  VÍA 4 — DOG.conide:
    ruesma.dog.conide = ctr.ide

  VÍA 5 — PFfir.graide:
    ruesma.PFfir.conide = ctr.ide -> PFfir.graide -> ruesma_rep.dbo.gra.ide

  VÍA 6 — PFfir.dogide:
    ruesma.PFfir.conide = ctr.ide -> PFfir.dogide -> ruesma.dog.ide

Además, con --pdf-name y/o --pdf-code ejecuta una comprobación inversa:

  1) Busca el documento en ruesma_rep.dbo.gra por cod/res/nom/nomori.
  2) Muestra qué relaciones apuntan a ese gra.ide:
       - rcg.gra
       - PFfir.graide
       - acugra.graide
       - k_acd.graide
       - gra.graant, en ambos sentidos

Ejemplos:

    python scripts/diagnose_sigrid_contrato_gra.py

    python scripts/diagnose_sigrid_contrato_gra.py B86359866 0695

    python scripts/diagnose_sigrid_contrato_gra.py B86359866 0695 --download

    python scripts/diagnose_sigrid_contrato_gra.py B86359866 0695 \
        --pdf-name "SUMINISTROS_DE_OBRAS_MOSTOLES.PED1.r__1_.pdf"

    python scripts/diagnose_sigrid_contrato_gra.py B86359866 0695 \
        --pdf-code "202412170843089860.vmartin" --download
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any


# =============================================================================
# Configuración / utilidades generales
# =============================================================================


def load_dotenv_manually(env_path: Path) -> dict[str, str]:
    """Lee un .env sencillo KEY=VALUE sin depender de python-dotenv."""
    if not env_path.exists():
        return {}

    result: dict[str, str] = {}
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        result[key] = value
    return result


def normalize_obra_code(raw: str | None) -> str | None:
    """Normaliza códigos de obra tipo 695 -> 0695."""
    if raw is None:
        return None
    c = str(raw).strip()
    if not c or not c.isdigit():
        return None
    if len(c) == 3:
        return "0" + c
    if len(c) == 4 and c.startswith("0"):
        return c
    return c


def normalize_cif(raw: str | None) -> str | None:
    """Normaliza CIF/NIF para comparación básica."""
    if raw is None:
        return None
    c = str(raw).strip().upper()
    c = re.sub(r"[\s\-.]", "", c)
    return c or None


def cif_variants(cif: str) -> list[str]:
    """Devuelve variantes útiles: B123..., ESB123..."""
    out: list[str] = []

    def add(v: str | None) -> None:
        if v and v not in out:
            out.append(v)

    add(cif)
    if cif.startswith("ES") and len(cif) > 2:
        add(cif[2:])
    elif re.match(r"^[A-Z]\d", cif):
        add("ES" + cif)
    return out


def sanitize_filename(name: str) -> str:
    s = re.sub(r'[\\/:*?"<>|]', "_", str(name))
    s = s.strip().rstrip(".")
    return s or "documento"


def safe_db_name(name: str) -> str:
    """Valida y devuelve un nombre de base de datos entre corchetes.

    Los nombres de BD no pueden ir parametrizados en SQL Server, por eso se
    validan antes de interpolarlos.
    """
    if not re.match(r"^[A-Za-z0-9_]+$", name or ""):
        raise ValueError(f"Nombre de base de datos no seguro: {name!r}")
    return f"[{name}]"


def sql_norm(expr: str) -> str:
    """Expresión SQL Server para normalizar CIF/NIF."""
    return (
        "UPPER(REPLACE(REPLACE(REPLACE("
        f"ISNULL({expr}, ''), ' ', ''), '-', ''), '.', ''))"
    )


def placeholders(values: list[Any]) -> str:
    if not values:
        raise ValueError("No se pueden crear placeholders para lista vacía")
    return ",".join("?" for _ in values)


def rows_to_dicts(body: dict | None) -> list[dict[str, Any]]:
    if not body or not body.get("rows"):
        return []
    cols = body["columns"]
    return [dict(zip(cols, row)) for row in body["rows"]]


def size_label(size: int | None) -> str:
    n = int(size or 0)
    if n >= 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.0f} KB"
    if n > 0:
        return f"{n} bytes"
    return "SIN BINARIO"


# =============================================================================
# Cliente API SIGRID
# =============================================================================


try:
    import httpx
except ImportError:
    print("❌ Falta httpx. Instala con: pip install httpx")
    sys.exit(4)


class SigridApi:
    def __init__(
        self,
        *,
        base_url: str,
        function_key: str,
        database: str,
        database_rep: str,
        timeout_s: float,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.function_key = function_key
        self.database = database
        self.database_rep = database_rep
        self.timeout_s = timeout_s

    def run_query(
        self,
        label: str,
        sql: str,
        parameters: list[Any] | None = None,
        *,
        max_rows: int = 500,
        target_database: str | None = None,
    ) -> dict | None:
        db = target_database or self.database
        url = f"{self.base_url}/api/sql/read"
        payload = {
            "database": db,
            "sql": sql,
            "parameters": parameters or [],
            "timeout_seconds": int(self.timeout_s),
            "max_rows": max_rows,
        }
        headers = {
            "x-functions-key": self.function_key,
            "Content-Type": "application/json",
        }

        print(f"\n{'─' * 78}")
        print(f"  📡 {label}")
        print(f"{'─' * 78}")
        print(f"  db={db}  params={parameters or []}  max_rows={max_rows}")

        try:
            with httpx.Client(timeout=self.timeout_s) as client:
                r = client.post(url, json=payload, headers=headers)
        except Exception as exc:
            print(f"  ❌ Error de conexión: {exc!r}")
            return None

        print(f"  HTTP {r.status_code}")
        if r.status_code != 200:
            print(f"  Body: {r.text[:600]}")
            return None

        try:
            body = r.json()
        except json.JSONDecodeError:
            print("  ❌ Respuesta JSON inválida")
            print(r.text[:600])
            return None

        if not body.get("ok"):
            print(f"  ❌ ok=false: {json.dumps(body, ensure_ascii=False)[:900]}")
            return None

        rc = body.get("row_count", 0)
        tr = body.get("truncated", False)
        print(f"  ✅ {rc} fila(s){' (TRUNCADO)' if tr else ''}")
        return body

    def download_document(
        self,
        *,
        store: str,
        doc_id: int,
        out_dir: Path,
        fallback_name: str,
    ) -> bool:
        """Descarga un documento de gra o dog vía /api/documents/read."""
        if store == "gra":
            payload = {
                "database": self.database_rep,
                "schema": "dbo",
                "table": "gra",
                "id_column": "ide",
                "id_value": doc_id,
                "blob_column": "ima",
                # En la captura el nombre visible del PDF está en gra.res.
                "filename_columns": ["res", "nomori", "nom", "cod"],
                "disposition": "attachment",
            }
        elif store == "dog":
            payload = {
                "database": self.database,
                "schema": "dbo",
                "table": "dog",
                "id_column": "ide",
                "id_value": doc_id,
                "blob_column": "graima",
                "filename_columns": ["nomori", "granom", "codrep"],
                "disposition": "attachment",
            }
        else:
            print(f"    ❌ Almacén desconocido: {store!r}")
            return False

        url = f"{self.base_url}/api/documents/read"
        headers = {
            "x-functions-key": self.function_key,
            "Content-Type": "application/json",
        }

        try:
            with httpx.Client(timeout=120) as client:
                r = client.post(url, json=payload, headers=headers)
        except Exception as exc:
            print(f"    ❌ Error de descarga: {exc!r}")
            return False

        if r.status_code != 200:
            print(f"    ❌ HTTP {r.status_code}: {r.text[:400]}")
            return False

        binary = r.content
        if not binary:
            print("    ❌ Respuesta vacía")
            return False

        fname = sanitize_filename(r.headers.get("X-Document-Filename", fallback_name))

        # Si no viene extensión y el binario es PDF, añadimos .pdf.
        if not Path(fname).suffix and binary[:4] == b"%PDF":
            fname += ".pdf"

        out_path = out_dir / fname
        if out_path.exists():
            out_path = out_dir / f"{out_path.stem}_{store}{doc_id}{out_path.suffix}"

        out_path.write_bytes(binary)
        ct = r.headers.get("Content-Type", "?")
        print(f"    ✅ {out_path.name}  ({len(binary):,} bytes, {ct})")
        return True


# =============================================================================
# Impresión de resultados
# =============================================================================


def print_rows(title: str, rows: list[dict[str, Any]], max_rows: int = 30) -> None:
    print(f"\n{'━' * 78}")
    print(f"  {title}: {len(rows)} fila(s)")
    print(f"{'━' * 78}")
    if not rows:
        print("  Sin resultados.")
        return

    for i, row in enumerate(rows[:max_rows], start=1):
        print(f"\n  Fila {i}:")
        for k, v in row.items():
            print(f"    {k:<26} = {v!r}")
    if len(rows) > max_rows:
        print(f"\n  ... {len(rows) - max_rows} fila(s) más no mostradas.")


def print_doc(doc: dict[str, Any], i: int) -> None:
    print(f"\n  Documento {i}:")
    print(f"    vía             = {doc.get('_via')!r}")
    print(f"    almacén         = {doc.get('_store')}  (ide={doc.get('_ide')})")
    print(f"    nombre          = {doc.get('_name')!r}")
    print(f"    tipo documental = {doc.get('_tipo')!r}")
    print(f"    fecha           = {doc.get('_fec')!r}")
    print(f"    usuario         = {doc.get('_usu')!r}")
    print(f"    tamaño          = {size_label(doc.get('_size'))}")

    optional_keys = [
        ("_cod", "cod (gra)"),
        ("_res", "res (gra)"),
        ("_nom", "nom (gra)"),
        ("_nomori", "nomori (gra)"),
        ("_tex", "tex (gra)"),
        ("_codrep", "codrep (dog)"),
        ("_vin", "vin"),
        ("_rcg_clase", "rcg.cla"),
        ("_is_pdf", "es PDF"),
        ("_firma_estado", "firma_estado"),
        ("_firma_tipo", "firma_tipo"),
        ("_firma_fec", "firma_fec"),
        ("_firma_hor", "firma_hor"),
    ]
    for key, label in optional_keys:
        if doc.get(key) is not None:
            value = bool(doc[key]) if key == "_is_pdf" else doc[key]
            print(f"    {label:<15} = {value!r}")


# =============================================================================
# Comprobación inversa por nombre/código de PDF en gra
# =============================================================================


def run_pdf_reverse_check(
    api: SigridApi,
    *,
    pdf_name: str | None,
    pdf_code: str | None,
    max_rows: int,
) -> list[int]:
    """Busca gra por nombre/código y muestra relaciones que apuntan a él."""
    if not pdf_name and not pdf_code:
        return []

    rep_db = safe_db_name(api.database_rep)
    gra_table = f"{rep_db}.dbo.gra"

    where_parts: list[str] = []
    params: list[Any] = []

    if pdf_code:
        where_parts.append("g.cod = ?")
        params.append(pdf_code)

    if pdf_name:
        # Búsqueda exacta y parcial sobre los campos que pueden contener el nombre visible.
        where_parts.append(
            "("
            "g.res = ? OR g.nomori = ? OR g.nom = ? OR g.cod = ? "
            "OR g.res LIKE ? OR g.nomori LIKE ? OR g.nom LIKE ? OR g.cod LIKE ?"
            ")"
        )
        like_value = f"%{pdf_name}%"
        params.extend([
            pdf_name,
            pdf_name,
            pdf_name,
            pdf_name,
            like_value,
            like_value,
            like_value,
            like_value,
        ])

    where_sql = " OR ".join(where_parts)

    body = api.run_query(
        "CHECK PDF: buscar en gra por nombre/código",
        f"""\
SELECT TOP ({max_rows})
    g.ide                         AS gra_ide,
    g.cod                         AS gra_cod,
    g.res                         AS gra_res,
    g.nom                         AS gra_nom,
    g.nomori                      AS gra_nomori,
    g.tex                         AS gra_tex,
    g.cla                         AS gra_clave,
    g.fec                         AS gra_fec,
    g.usu                         AS gra_usu,
    g.vin                         AS gra_vin,
    g.guid                        AS gra_guid,
    g.gratipide                   AS gra_tipide,
    auxgra.cod                    AS tipo_cod,
    auxgra.res                    AS tipo_descripcion,
    g.graant                      AS gra_version_anterior,
    DATALENGTH(g.ima)             AS ima_bytes,
    CASE
        WHEN SUBSTRING(CAST(g.ima AS varbinary(max)), 1, 4) = 0x25504446
        THEN 1 ELSE 0
    END                           AS es_pdf_magic
FROM {gra_table} AS g
LEFT JOIN auxgra
    ON auxgra.ide = g.gratipide
WHERE {where_sql}
ORDER BY
    CASE
        WHEN g.cod = ? THEN 0
        WHEN g.res = ? THEN 1
        WHEN g.nomori = ? THEN 2
        WHEN g.nom = ? THEN 3
        ELSE 9
    END,
    g.fec DESC,
    g.ide DESC
""",
        params + [pdf_code or pdf_name or "", pdf_name or "", pdf_name or "", pdf_name or ""],
        max_rows=max_rows,
    )

    gra_matches = rows_to_dicts(body)
    print_rows("CHECK PDF: coincidencias encontradas en gra", gra_matches, max_rows=20)

    gra_ids = [int(r["gra_ide"]) for r in gra_matches if r.get("gra_ide") is not None]
    if not gra_ids:
        print("\n  ⚠️  No se encontró ningún gra por ese nombre/código.")
        return []

    ids_csv = ",".join(str(x) for x in gra_ids)

    # ------------------------------------------------------------------
    # Relaciones rcg.gra
    # ------------------------------------------------------------------
    body_rcg = api.run_query(
        "CHECK PDF: relaciones rcg.gra -> concepto/contrato",
        f"""\
SELECT
    r.gra                         AS gra_ide,
    r.ide                         AS rcg_ide,
    r.con                         AS concepto_ide,
    r.pos                         AS rcg_pos,
    r.cla                         AS rcg_clase,

    con_x.cod                     AS concepto_cod,
    con_x.res                     AS concepto_res,

    ctr.ide                       AS contrato_ide,
    con_ctr.cod                   AS codigo_contrato,
    con_ctr.res                   AS nombre_contrato,
    ctr.fecdoc                    AS fecha_contrato,
    ctr.entcif                    AS ctr_cif,
    ctr.entres                    AS ctr_proveedor,
    ctr.entref                    AS ctr_referencia_proveedor,

    con_obr.cod                   AS codigo_obra,
    con_obr.res                   AS nombre_obra,

    prv.ide                       AS proveedor_ide,
    prv.cif                       AS prv_cif,
    prv.raz                       AS prv_razon_social,

    g.cod                         AS gra_cod,
    g.res                         AS gra_res,
    g.nom                         AS gra_nom,
    g.nomori                      AS gra_nomori,
    DATALENGTH(g.ima)             AS ima_bytes,
    CASE
        WHEN SUBSTRING(CAST(g.ima AS varbinary(max)), 1, 4) = 0x25504446
        THEN 1 ELSE 0
    END                           AS es_pdf_magic
FROM rcg AS r
JOIN {gra_table} AS g
    ON g.ide = r.gra
LEFT JOIN [con] AS con_x
    ON con_x.ide = r.con
LEFT JOIN ctr
    ON ctr.ide = r.con
LEFT JOIN [con] AS con_ctr
    ON con_ctr.ide = ctr.ide
LEFT JOIN [con] AS con_obr
    ON con_obr.ide = ctr.obride
LEFT JOIN prv
    ON prv.ide = ctr.entide
WHERE r.gra IN ({ids_csv})
ORDER BY r.gra, r.pos, r.ide
""",
        [],
        max_rows=max_rows,
    )
    print_rows("CHECK PDF: relaciones encontradas en rcg", rows_to_dicts(body_rcg), max_rows=40)

    # ------------------------------------------------------------------
    # Relaciones PFfir.graide
    # ------------------------------------------------------------------
    body_pf = api.run_query(
        "CHECK PDF: relaciones PFfir.graide -> concepto/contrato",
        f"""\
SELECT
    pf.graide                     AS gra_ide,
    pf.ide                        AS pffir_ide,
    pf.conide                     AS concepto_ide,
    pf.dogide                     AS dog_ide,
    pf.tipfir                     AS firma_tipo,
    pf.estfir                     AS firma_estado,
    pf.fec                        AS firma_fec,
    pf.hor                        AS firma_hor,

    con_x.cod                     AS concepto_cod,
    con_x.res                     AS concepto_res,

    ctr.ide                       AS contrato_ide,
    con_ctr.cod                   AS codigo_contrato,
    con_ctr.res                   AS nombre_contrato,
    ctr.fecdoc                    AS fecha_contrato,
    ctr.entcif                    AS ctr_cif,
    ctr.entres                    AS ctr_proveedor,

    con_obr.cod                   AS codigo_obra,
    con_obr.res                   AS nombre_obra,

    prv.ide                       AS proveedor_ide,
    prv.cif                       AS prv_cif,
    prv.raz                       AS prv_razon_social,

    g.cod                         AS gra_cod,
    g.res                         AS gra_res,
    g.nom                         AS gra_nom,
    g.nomori                      AS gra_nomori,
    DATALENGTH(g.ima)             AS ima_bytes,
    CASE
        WHEN SUBSTRING(CAST(g.ima AS varbinary(max)), 1, 4) = 0x25504446
        THEN 1 ELSE 0
    END                           AS es_pdf_magic
FROM [PFfir] AS pf
JOIN {gra_table} AS g
    ON g.ide = pf.graide
LEFT JOIN [con] AS con_x
    ON con_x.ide = pf.conide
LEFT JOIN ctr
    ON ctr.ide = pf.conide
LEFT JOIN [con] AS con_ctr
    ON con_ctr.ide = ctr.ide
LEFT JOIN [con] AS con_obr
    ON con_obr.ide = ctr.obride
LEFT JOIN prv
    ON prv.ide = ctr.entide
WHERE pf.graide IN ({ids_csv})
ORDER BY pf.graide, pf.fec DESC, pf.hor DESC, pf.ide DESC
""",
        [],
        max_rows=max_rows,
    )
    print_rows("CHECK PDF: relaciones encontradas en PFfir", rows_to_dicts(body_pf), max_rows=40)

    # ------------------------------------------------------------------
    # Relaciones acugra.graide
    # ------------------------------------------------------------------
    body_acugra = api.run_query(
        "CHECK PDF: relaciones acugra.graide",
        f"""\
SELECT
    a.graide                      AS gra_ide,
    a.acuide                      AS acuerdo_ide,
    a.pos                         AS acugra_pos,
    g.cod                         AS gra_cod,
    g.res                         AS gra_res,
    g.nom                         AS gra_nom,
    g.nomori                      AS gra_nomori,
    DATALENGTH(g.ima)             AS ima_bytes
FROM acugra AS a
JOIN {gra_table} AS g
    ON g.ide = a.graide
WHERE a.graide IN ({ids_csv})
ORDER BY a.graide, a.pos
""",
        [],
        max_rows=max_rows,
    )
    print_rows("CHECK PDF: relaciones encontradas en acugra", rows_to_dicts(body_acugra), max_rows=40)

    # ------------------------------------------------------------------
    # Relaciones k_acd.graide
    # ------------------------------------------------------------------
    body_kacd = api.run_query(
        "CHECK PDF: relaciones k_acd.graide",
        f"""\
SELECT
    k.graide                      AS gra_ide,
    k.aceide                      AS acto_administrativo_ide,
    k.dogide                      AS dog_ide,
    g.cod                         AS gra_cod,
    g.res                         AS gra_res,
    g.nom                         AS gra_nom,
    g.nomori                      AS gra_nomori,
    DATALENGTH(g.ima)             AS ima_bytes
FROM k_acd AS k
JOIN {gra_table} AS g
    ON g.ide = k.graide
WHERE k.graide IN ({ids_csv})
ORDER BY k.graide, k.aceide
""",
        [],
        max_rows=max_rows,
    )
    print_rows("CHECK PDF: relaciones encontradas en k_acd", rows_to_dicts(body_kacd), max_rows=40)

    # ------------------------------------------------------------------
    # Versiones gra.graant en ambos sentidos
    # ------------------------------------------------------------------
    body_versions = api.run_query(
        "CHECK PDF: relaciones gra.graant/versiones",
        f"""\
SELECT
    'ESTE_GRA_APUNTA_A_ANTERIOR'  AS sentido,
    g.ide                         AS gra_ide,
    g.cod                         AS gra_cod,
    g.res                         AS gra_res,
    g.graant                      AS gra_relacionado_ide,
    prev.cod                      AS gra_relacionado_cod,
    prev.res                      AS gra_relacionado_res,
    DATALENGTH(prev.ima)          AS gra_relacionado_bytes
FROM {gra_table} AS g
LEFT JOIN {gra_table} AS prev
    ON prev.ide = g.graant
WHERE
    g.ide IN ({ids_csv})
    AND ISNULL(g.graant, 0) <> 0

UNION ALL

SELECT
    'OTRO_GRA_APUNTA_A_ESTE_COMO_ANTERIOR' AS sentido,
    base.ide                      AS gra_ide,
    base.cod                      AS gra_cod,
    base.res                      AS gra_res,
    child.ide                     AS gra_relacionado_ide,
    child.cod                     AS gra_relacionado_cod,
    child.res                     AS gra_relacionado_res,
    DATALENGTH(child.ima)         AS gra_relacionado_bytes
FROM {gra_table} AS base
JOIN {gra_table} AS child
    ON child.graant = base.ide
WHERE base.ide IN ({ids_csv})
ORDER BY gra_ide, sentido, gra_relacionado_ide
""",
        [],
        max_rows=max_rows,
    )
    print_rows("CHECK PDF: versiones encontradas en gra.graant", rows_to_dicts(body_versions), max_rows=40)

    return gra_ids


# =============================================================================
# Búsqueda de contrato y documentos asociados
# =============================================================================


def find_contracts(api: SigridApi, *, cif: str, obra: str, max_rows: int) -> list[dict[str, Any]]:
    variants = cif_variants(cif)
    in_sql = placeholders(variants)
    params: list[Any] = [obra] + variants + variants

    body = api.run_query(
        "Q1: Localizar contrato por CIF/NIF y obra",
        f"""\
SELECT TOP ({max_rows})
    ctr.ide             AS contrato_ide,
    con_ctr.cod         AS codigo_contrato,
    con_ctr.res         AS nombre_contrato,
    ctr.fecdoc          AS fecha_contrato,
    ctr.entcif          AS cif_proveedor_ctr,
    ctr.entres          AS nombre_proveedor_ctr,
    ctr.entref          AS referencia_proveedor,
    ctr.entide          AS proveedor_ide_ctr,

    prv.ide             AS proveedor_ide_prv,
    prv.cif             AS cif_proveedor_prv,
    prv.raz             AS razon_social_prv,

    ctr.obride          AS obra_ide,
    con_obr.cod         AS codigo_obra,
    con_obr.res         AS nombre_obra

FROM ctr
JOIN [con] AS con_ctr
    ON ctr.ide = con_ctr.ide
JOIN [con] AS con_obr
    ON ctr.obride = con_obr.ide
LEFT JOIN prv
    ON ctr.entide = prv.ide
WHERE
    con_obr.cod = ?
    AND (
        {sql_norm('prv.cif')} IN ({in_sql})
        OR {sql_norm('ctr.entcif')} IN ({in_sql})
    )
ORDER BY
    ctr.fecdoc DESC,
    ctr.ide DESC
""",
        params,
        max_rows=max_rows,
    )
    return rows_to_dicts(body)


def collect_contract_documents(
    api: SigridApi,
    *,
    contrato_ides: list[int],
    max_rows: int,
) -> list[dict[str, Any]]:
    if not contrato_ides:
        return []

    rep_db = safe_db_name(api.database_rep)
    gra_table = f"{rep_db}.dbo.gra"
    ides_csv = ",".join(str(i) for i in contrato_ides)
    all_docs: list[dict[str, Any]] = []

    # ------------------------------------------------------------------
    # VÍA 1: RCG -> gra
    # ------------------------------------------------------------------
    body_v1 = api.run_query(
        "VÍA 1: RCG -> gra + auxgra",
        f"""\
SELECT
    rcg.con            AS contrato_ide,
    rcg.gra            AS gra_ide,
    rcg.pos            AS rcg_pos,
    rcg.cla            AS rcg_clase,

    gra_rep.cod        AS gra_cod,
    gra_rep.res        AS gra_res,
    gra_rep.tex        AS gra_tex,
    gra_rep.cla        AS gra_clave,
    gra_rep.nom        AS gra_nom,
    gra_rep.nomori     AS gra_nomori,
    gra_rep.fec        AS gra_fec,
    gra_rep.usu        AS gra_usu,
    gra_rep.vin        AS gra_vin,
    gra_rep.guid       AS gra_guid,
    gra_rep.gratipide  AS gra_tipide,
    gra_rep.graant     AS gra_version_anterior,

    auxgra.cod         AS tipo_cod,
    auxgra.res         AS tipo_descripcion,

    DATALENGTH(gra_rep.ima) AS ima_bytes,

    CASE
        WHEN SUBSTRING(CAST(gra_rep.ima AS varbinary(max)), 1, 4) = 0x25504446
        THEN 1 ELSE 0
    END AS es_pdf_magic
FROM rcg
JOIN {gra_table} AS gra_rep
    ON rcg.gra = gra_rep.ide
LEFT JOIN auxgra
    ON gra_rep.gratipide = auxgra.ide
WHERE rcg.con IN ({ides_csv})
ORDER BY rcg.con, rcg.pos
""",
        [],
        max_rows=max_rows,
    )

    for doc in rows_to_dicts(body_v1):
        all_docs.append({
            "_via": "RCG→gra",
            "_store": "gra",
            "_ide": doc["gra_ide"],
            "_name": doc.get("gra_res") or doc.get("gra_nomori") or doc.get("gra_nom") or doc.get("gra_cod") or "?",
            "_size": doc.get("ima_bytes") or 0,
            "_tipo": doc.get("tipo_descripcion") or doc.get("tipo_cod") or "?",
            "_fec": doc.get("gra_fec"),
            "_usu": doc.get("gra_usu"),
            "_vin": doc.get("gra_vin"),
            "_cod": doc.get("gra_cod"),
            "_res": doc.get("gra_res"),
            "_nom": doc.get("gra_nom"),
            "_nomori": doc.get("gra_nomori"),
            "_tex": doc.get("gra_tex"),
            "_clave": doc.get("gra_clave"),
            "_contrato_ide": doc.get("contrato_ide"),
            "_rcg_clase": doc.get("rcg_clase"),
            "_is_pdf": doc.get("es_pdf_magic"),
        })

    # ------------------------------------------------------------------
    # VÍA 2: DOG.ctride = ctr.ide
    # ------------------------------------------------------------------
    body_v2 = api.run_query(
        "VÍA 2: DOG.ctride -> dog",
        f"""\
SELECT
    dog.ctride      AS contrato_ide,
    dog.ide         AS dog_ide,
    dog.granom      AS dog_nom,
    dog.nomori      AS dog_nomori,
    dog.gratam      AS dog_tam,
    dog.fec         AS dog_fec,
    dog.usu         AS dog_usu,
    dog.obride      AS dog_obride,
    dog.conide      AS dog_conide,
    dog.codrep      AS dog_codrep,
    dog.guid        AS dog_guid,
    dog.auxdopide   AS dog_tipide,
    auxdop.cod      AS tipo_cod,
    auxdop.res      AS tipo_descripcion,
    DATALENGTH(dog.graima) AS graima_bytes,
    CASE
        WHEN SUBSTRING(CAST(dog.graima AS varbinary(max)), 1, 4) = 0x25504446
        THEN 1 ELSE 0
    END AS es_pdf_magic
FROM dog
LEFT JOIN auxdop
    ON dog.auxdopide = auxdop.ide
WHERE dog.ctride IN ({ides_csv})
ORDER BY dog.ctride, dog.fec
""",
        [],
        max_rows=max_rows,
    )

    for doc in rows_to_dicts(body_v2):
        all_docs.append({
            "_via": "DOG.ctride",
            "_store": "dog",
            "_ide": doc["dog_ide"],
            "_name": doc.get("dog_nomori") or doc.get("dog_nom") or doc.get("dog_codrep") or "?",
            "_size": doc.get("graima_bytes") or doc.get("dog_tam") or 0,
            "_tipo": doc.get("tipo_descripcion") or doc.get("tipo_cod") or "?",
            "_fec": doc.get("dog_fec"),
            "_usu": doc.get("dog_usu"),
            "_codrep": doc.get("dog_codrep"),
            "_contrato_ide": doc.get("contrato_ide"),
            "_is_pdf": doc.get("es_pdf_magic"),
        })

    # ------------------------------------------------------------------
    # VÍA 3: CONDOG.conide = ctr.ide -> dog
    # ------------------------------------------------------------------
    body_v3 = api.run_query(
        "VÍA 3: CONDOG -> dog",
        f"""\
SELECT
    condog.conide   AS contrato_ide,
    condog.pos      AS condog_pos,
    condog.cla      AS condog_clase,
    dog.ide         AS dog_ide,
    dog.granom      AS dog_nom,
    dog.nomori      AS dog_nomori,
    dog.gratam      AS dog_tam,
    dog.fec         AS dog_fec,
    dog.usu         AS dog_usu,
    dog.obride      AS dog_obride,
    dog.conide      AS dog_conide,
    dog.ctride      AS dog_ctride,
    dog.codrep      AS dog_codrep,
    dog.guid        AS dog_guid,
    dog.auxdopide   AS dog_tipide,
    auxdop.cod      AS tipo_cod,
    auxdop.res      AS tipo_descripcion,
    DATALENGTH(dog.graima) AS graima_bytes,
    CASE
        WHEN SUBSTRING(CAST(dog.graima AS varbinary(max)), 1, 4) = 0x25504446
        THEN 1 ELSE 0
    END AS es_pdf_magic
FROM condog
JOIN dog
    ON condog.dogide = dog.ide
LEFT JOIN auxdop
    ON dog.auxdopide = auxdop.ide
WHERE condog.conide IN ({ides_csv})
ORDER BY condog.conide, condog.pos
""",
        [],
        max_rows=max_rows,
    )

    for doc in rows_to_dicts(body_v3):
        all_docs.append({
            "_via": "CONDOG→dog",
            "_store": "dog",
            "_ide": doc["dog_ide"],
            "_name": doc.get("dog_nomori") or doc.get("dog_nom") or doc.get("dog_codrep") or "?",
            "_size": doc.get("graima_bytes") or doc.get("dog_tam") or 0,
            "_tipo": doc.get("tipo_descripcion") or doc.get("tipo_cod") or "?",
            "_fec": doc.get("dog_fec"),
            "_usu": doc.get("dog_usu"),
            "_codrep": doc.get("dog_codrep"),
            "_contrato_ide": doc.get("contrato_ide"),
            "_is_pdf": doc.get("es_pdf_magic"),
        })

    # ------------------------------------------------------------------
    # VÍA 4: DOG.conide = ctr.ide
    # ------------------------------------------------------------------
    body_v4 = api.run_query(
        "VÍA 4: DOG.conide = ctr.ide",
        f"""\
SELECT
    dog.conide      AS contrato_ide,
    dog.ide         AS dog_ide,
    dog.granom      AS dog_nom,
    dog.nomori      AS dog_nomori,
    dog.gratam      AS dog_tam,
    dog.fec         AS dog_fec,
    dog.usu         AS dog_usu,
    dog.ctride      AS dog_ctride,
    dog.obride      AS dog_obride,
    dog.codrep      AS dog_codrep,
    dog.guid        AS dog_guid,
    dog.auxdopide   AS dog_tipide,
    auxdop.cod      AS tipo_cod,
    auxdop.res      AS tipo_descripcion,
    DATALENGTH(dog.graima) AS graima_bytes,
    CASE
        WHEN SUBSTRING(CAST(dog.graima AS varbinary(max)), 1, 4) = 0x25504446
        THEN 1 ELSE 0
    END AS es_pdf_magic
FROM dog
LEFT JOIN auxdop
    ON dog.auxdopide = auxdop.ide
WHERE dog.conide IN ({ides_csv})
ORDER BY dog.conide, dog.fec
""",
        [],
        max_rows=max_rows,
    )

    for doc in rows_to_dicts(body_v4):
        all_docs.append({
            "_via": "DOG.conide",
            "_store": "dog",
            "_ide": doc["dog_ide"],
            "_name": doc.get("dog_nomori") or doc.get("dog_nom") or doc.get("dog_codrep") or "?",
            "_size": doc.get("graima_bytes") or doc.get("dog_tam") or 0,
            "_tipo": doc.get("tipo_descripcion") or doc.get("tipo_cod") or "?",
            "_fec": doc.get("dog_fec"),
            "_usu": doc.get("dog_usu"),
            "_codrep": doc.get("dog_codrep"),
            "_contrato_ide": doc.get("contrato_ide"),
            "_is_pdf": doc.get("es_pdf_magic"),
        })

    # ------------------------------------------------------------------
    # VÍA 5: PFfir.graide -> gra
    # ------------------------------------------------------------------
    body_v5 = api.run_query(
        "VÍA 5: PFfir.graide -> gra",
        f"""\
SELECT
    pf.conide          AS contrato_ide,
    pf.ide             AS pffir_ide,
    pf.tipfir          AS firma_tipo,
    pf.estfir          AS firma_estado,
    pf.fec             AS firma_fec,
    pf.hor             AS firma_hor,

    pf.graide          AS gra_ide,

    gra_rep.cod        AS gra_cod,
    gra_rep.res        AS gra_res,
    gra_rep.tex        AS gra_tex,
    gra_rep.cla        AS gra_clave,
    gra_rep.nom        AS gra_nom,
    gra_rep.nomori     AS gra_nomori,
    gra_rep.fec        AS gra_fec,
    gra_rep.usu        AS gra_usu,
    gra_rep.vin        AS gra_vin,
    gra_rep.guid       AS gra_guid,
    gra_rep.gratipide  AS gra_tipide,
    gra_rep.graant     AS gra_version_anterior,

    auxgra.cod         AS tipo_cod,
    auxgra.res         AS tipo_descripcion,

    DATALENGTH(gra_rep.ima) AS ima_bytes,

    CASE
        WHEN SUBSTRING(CAST(gra_rep.ima AS varbinary(max)), 1, 4) = 0x25504446
        THEN 1 ELSE 0
    END AS es_pdf_magic
FROM [PFfir] AS pf
JOIN {gra_table} AS gra_rep
    ON pf.graide = gra_rep.ide
LEFT JOIN auxgra
    ON gra_rep.gratipide = auxgra.ide
WHERE
    pf.conide IN ({ides_csv})
    AND pf.graide IS NOT NULL
    AND pf.graide <> 0
ORDER BY
    pf.conide,
    pf.fec DESC,
    pf.hor DESC,
    pf.ide DESC
""",
        [],
        max_rows=max_rows,
    )

    for doc in rows_to_dicts(body_v5):
        all_docs.append({
            "_via": "PFfir.graide→gra",
            "_store": "gra",
            "_ide": doc["gra_ide"],
            "_name": doc.get("gra_res") or doc.get("gra_nomori") or doc.get("gra_nom") or doc.get("gra_cod") or "?",
            "_size": doc.get("ima_bytes") or 0,
            "_tipo": doc.get("tipo_descripcion") or doc.get("tipo_cod") or "?",
            "_fec": doc.get("gra_fec"),
            "_usu": doc.get("gra_usu"),
            "_vin": doc.get("gra_vin"),
            "_cod": doc.get("gra_cod"),
            "_res": doc.get("gra_res"),
            "_nom": doc.get("gra_nom"),
            "_nomori": doc.get("gra_nomori"),
            "_tex": doc.get("gra_tex"),
            "_clave": doc.get("gra_clave"),
            "_contrato_ide": doc.get("contrato_ide"),
            "_is_pdf": doc.get("es_pdf_magic"),
            "_firma_estado": doc.get("firma_estado"),
            "_firma_tipo": doc.get("firma_tipo"),
            "_firma_fec": doc.get("firma_fec"),
            "_firma_hor": doc.get("firma_hor"),
        })

    # ------------------------------------------------------------------
    # VÍA 6: PFfir.dogide -> dog
    # ------------------------------------------------------------------
    body_v6 = api.run_query(
        "VÍA 6: PFfir.dogide -> dog",
        f"""\
SELECT
    pf.conide        AS contrato_ide,
    pf.ide           AS pffir_ide,
    pf.tipfir        AS firma_tipo,
    pf.estfir        AS firma_estado,
    pf.fec           AS firma_fec,
    pf.hor           AS firma_hor,

    dog.ide          AS dog_ide,
    dog.granom       AS dog_nom,
    dog.nomori       AS dog_nomori,
    dog.gratam       AS dog_tam,
    dog.fec          AS dog_fec,
    dog.usu          AS dog_usu,
    dog.obride       AS dog_obride,
    dog.conide       AS dog_conide,
    dog.ctride       AS dog_ctride,
    dog.codrep       AS dog_codrep,
    dog.guid         AS dog_guid,
    dog.auxdopide    AS dog_tipide,

    auxdop.cod       AS tipo_cod,
    auxdop.res       AS tipo_descripcion,

    DATALENGTH(dog.graima) AS graima_bytes,

    CASE
        WHEN SUBSTRING(CAST(dog.graima AS varbinary(max)), 1, 4) = 0x25504446
        THEN 1 ELSE 0
    END AS es_pdf_magic
FROM [PFfir] AS pf
JOIN dog
    ON pf.dogide = dog.ide
LEFT JOIN auxdop
    ON dog.auxdopide = auxdop.ide
WHERE
    pf.conide IN ({ides_csv})
    AND pf.dogide IS NOT NULL
    AND pf.dogide <> 0
ORDER BY
    pf.conide,
    pf.fec DESC,
    pf.hor DESC,
    pf.ide DESC
""",
        [],
        max_rows=max_rows,
    )

    for doc in rows_to_dicts(body_v6):
        all_docs.append({
            "_via": "PFfir.dogide→dog",
            "_store": "dog",
            "_ide": doc["dog_ide"],
            "_name": doc.get("dog_nomori") or doc.get("dog_nom") or doc.get("dog_codrep") or f"dog_{doc['dog_ide']}",
            "_size": doc.get("graima_bytes") or doc.get("dog_tam") or 0,
            "_tipo": doc.get("tipo_descripcion") or doc.get("tipo_cod") or "?",
            "_fec": doc.get("dog_fec"),
            "_usu": doc.get("dog_usu"),
            "_codrep": doc.get("dog_codrep"),
            "_contrato_ide": doc.get("contrato_ide"),
            "_is_pdf": doc.get("es_pdf_magic"),
            "_firma_estado": doc.get("firma_estado"),
            "_firma_tipo": doc.get("firma_tipo"),
            "_firma_fec": doc.get("firma_fec"),
            "_firma_hor": doc.get("firma_hor"),
        })

    # Deduplicar por almacén + ide, conservando todas las vías encontradas.
    seen: dict[tuple[str, int], dict[str, Any]] = {}
    for doc in all_docs:
        key = (str(doc["_store"]), int(doc["_ide"]))
        if key not in seen:
            seen[key] = doc
        else:
            existing = seen[key]
            new_via = str(doc["_via"])
            existing_via = str(existing["_via"])
            if new_via not in existing_via:
                existing["_via"] = f"{existing_via} + {new_via}"
            # Si alguna vía marca que es PDF o tiene tamaño, preservarlo.
            if doc.get("_is_pdf") and not existing.get("_is_pdf"):
                existing["_is_pdf"] = doc.get("_is_pdf")
            if (doc.get("_size") or 0) > (existing.get("_size") or 0):
                existing["_size"] = doc.get("_size")

    return list(seen.values())


# =============================================================================
# Main
# =============================================================================


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Diagnóstico SIGRID de documentos PDF asociados a contrato de compra."
    )
    p.add_argument("cif", nargs="?", default="B86359866", help="CIF/NIF proveedor. Por defecto: B86359866")
    p.add_argument("obra", nargs="?", default="0695", help="Código de obra. Por defecto: 0695")
    p.add_argument("--download", action="store_true", help="Descarga los documentos encontrados con binario")
    p.add_argument("--pdf-name", default=None, help="Nombre del PDF para comprobación inversa en gra.res/nom/nomori/cod")
    p.add_argument("--pdf-code", default=None, help="Código gra.cod para comprobación inversa, por ejemplo 202412170843089860.vmartin")
    p.add_argument("--max-rows", type=int, default=200, help="Máximo de filas por consulta. Por defecto: 200")
    return p


def main() -> int:
    args = build_arg_parser().parse_args()

    project_root = Path(__file__).resolve().parent.parent
    env_path = project_root / ".env"
    env = load_dotenv_manually(env_path)

    def get_cfg(name: str) -> str | None:
        return os.environ.get(name) or env.get(name)

    base_url = get_cfg("SIGRID_API_BASE_URL")
    function_key = get_cfg("SIGRID_API_FUNCTION_KEY")
    database = get_cfg("SIGRID_API_DATABASE") or "ruesma"
    database_rep = get_cfg("SIGRID_API_DATABASE_REP") or "ruesma_rep"
    timeout_s = float(get_cfg("SIGRID_API_TIMEOUT_S") or "30")

    cif = normalize_cif(args.cif)
    obra = normalize_obra_code(args.obra)

    print("=" * 78)
    print(" DIAGNÓSTICO SIGRID — DOCUMENTOS DE CONTRATO")
    print("=" * 78)
    print(f"Raíz detectada : {project_root}")
    print(f"Ruta del .env  : {env_path}")
    print(f"¿Existe?       : {env_path.exists()}")
    print("-" * 78)
    print("Variables detectadas:")
    print(f"  SIGRID_API_BASE_URL     = {base_url!r}")
    if function_key:
        print(
            "  SIGRID_API_FUNCTION_KEY = "
            f"<presente, len={len(function_key)}, últimos 4={function_key[-4:]!r}>"
        )
    else:
        print(f"  SIGRID_API_FUNCTION_KEY = {function_key!r}")
    print(f"  SIGRID_API_DATABASE     = {database!r}")
    print(f"  SIGRID_API_DATABASE_REP = {database_rep!r}")
    print(f"  SIGRID_API_TIMEOUT_S    = {timeout_s}")
    print("-" * 78)

    problems: list[str] = []
    if not base_url:
        problems.append("❌ SIGRID_API_BASE_URL vacío o no definido")
    elif not base_url.startswith(("http://", "https://")):
        problems.append("❌ SIGRID_API_BASE_URL no empieza por http(s)://")
    if not function_key:
        problems.append("❌ SIGRID_API_FUNCTION_KEY vacío o no definido")
    if not cif:
        problems.append("❌ CIF inválido")
    if not obra:
        problems.append("❌ Código de obra inválido")

    try:
        safe_db_name(database)
        safe_db_name(database_rep)
    except ValueError as exc:
        problems.append(f"❌ {exc}")

    if problems:
        for p in problems:
            print(f"  {p}")
        return 2

    print("✅ Configuración OK")
    print("-" * 78)
    print("Parámetros:")
    print(f"  cif        = {cif!r}")
    print(f"  cif_vars   = {cif_variants(cif)!r}")
    print(f"  obra       = {obra!r}")
    print(f"  --download = {args.download}")
    print(f"  --pdf-name = {args.pdf_name!r}")
    print(f"  --pdf-code = {args.pdf_code!r}")
    print("-" * 78)

    api = SigridApi(
        base_url=base_url or "",
        function_key=function_key or "",
        database=database,
        database_rep=database_rep,
        timeout_s=timeout_s,
    )

    # 0) Comprobación inversa por PDF, si se pidió.
    gra_ids_from_pdf_check = run_pdf_reverse_check(
        api,
        pdf_name=args.pdf_name,
        pdf_code=args.pdf_code,
        max_rows=args.max_rows,
    )

    # 1) Localizar contrato por obra + CIF.
    contratos = find_contracts(api, cif=cif, obra=obra, max_rows=args.max_rows)

    if not contratos:
        print(f"\n⚠️  No se encontró contrato para CIF={cif!r} obra={obra!r}.")
        print("\nSugerencias:")
        print("  1) Revisa si el CIF está con país delante, por ejemplo ESB86359866.")
        print("  2) Revisa si la obra está guardada como 695 o 0695.")
        print("  3) Ejecuta con --pdf-name para comprobar el documento desde gra.")
        print("=" * 78)
        return 0

    print(f"\n{'━' * 78}")
    print(f"  CONTRATOS ENCONTRADOS: {len(contratos)}")
    print(f"{'━' * 78}")

    contrato_ides: list[int] = []
    for c in contratos:
        cid = int(c["contrato_ide"])
        contrato_ides.append(cid)
        print(
            f"  ide={cid}  cod={c.get('codigo_contrato')!r}  "
            f"fecha={c.get('fecha_contrato')!r}  "
            f"obra={c.get('codigo_obra')!r}  "
            f"prov={c.get('nombre_proveedor_ctr') or c.get('razon_social_prv')!r}  "
            f"cif_ctr={c.get('cif_proveedor_ctr')!r}  cif_prv={c.get('cif_proveedor_prv')!r}"
        )

    # 2) Recopilar documentos asociados al contrato por todas las vías.
    unique_docs = collect_contract_documents(
        api,
        contrato_ides=contrato_ides,
        max_rows=args.max_rows,
    )

    print(f"\n\n{'━' * 78}")
    print(f"  📎 DOCUMENTOS ENCONTRADOS: {len(unique_docs)}")
    print(f"{'━' * 78}")

    docs_con_binario: list[dict[str, Any]] = []
    if not unique_docs:
        print("\n  ❌ No se encontró ningún documento por ninguna vía.")
        print("\n  Sugerencias:")
        print("    1) Revisa si la ventana de gráficos está asociada a otro concepto.")
        print("    2) Ejecuta con --pdf-name para ver desde gra qué relaciones aparecen.")
    else:
        for i, doc in enumerate(unique_docs, start=1):
            print_doc(doc, i)
            if (doc.get("_size") or 0) > 0:
                docs_con_binario.append(doc)

        print(f"\n  {'─' * 74}")
        print("  📊 Resumen por vía:")
        via_counts = Counter(str(d.get("_via")) for d in unique_docs)
        for via, count in via_counts.items():
            print(f"    {via:<42} {count} doc(s)")
        print(f"\n  Total con binario: {len(docs_con_binario)}")
        print(f"  Total sin binario: {len(unique_docs) - len(docs_con_binario)}")

    # 3) Descarga opcional.
    if docs_con_binario and args.download:
        codigo_ctr = contratos[0].get("codigo_contrato") or "sin_codigo"
        download_dir = Path.home() / "Downloads" / "sigrid_docs" / sanitize_filename(str(codigo_ctr))
        download_dir.mkdir(parents=True, exist_ok=True)

        print(f"\n{'━' * 78}")
        print(f"  ⬇️  DESCARGANDO {len(docs_con_binario)} documento(s)")
        print(f"  📁 {download_dir}")
        print(f"{'━' * 78}")

        ok = 0
        fail = 0
        for idx, doc in enumerate(docs_con_binario, start=1):
            store = str(doc["_store"])
            doc_id = int(doc["_ide"])
            name = str(doc.get("_name") or f"doc_{doc_id}")
            via = str(doc.get("_via"))

            print(f"\n  [{idx}/{len(docs_con_binario)}] {store}.ide={doc_id}  vía={via}  nom={name!r}")
            success = api.download_document(
                store=store,
                doc_id=doc_id,
                out_dir=download_dir,
                fallback_name=name,
            )
            if success:
                ok += 1
            else:
                fail += 1

        print(f"\n{'━' * 78}")
        print(f"  📊 DESCARGA: {ok} OK, {fail} fallidos de {len(docs_con_binario)}")
        print(f"  📁 {download_dir}")
        if ok > 0:
            print(f"\n  explorer \"{download_dir}\"")

    elif docs_con_binario and not args.download:
        codigo_ctr = contratos[0].get("codigo_contrato") or "sin_codigo"
        destino = Path.home() / "Downloads" / "sigrid_docs" / sanitize_filename(str(codigo_ctr))
        print(f"\n  ℹ️  {len(docs_con_binario)} doc(s) descargables. Usa --download:")
        print(f"    python scripts/diagnose_sigrid_contrato_gra.py {cif} {obra} --download")
        if args.pdf_name:
            print(f"    python scripts/diagnose_sigrid_contrato_gra.py {cif} {obra} --pdf-name \"{args.pdf_name}\" --download")
        if args.pdf_code:
            print(f"    python scripts/diagnose_sigrid_contrato_gra.py {cif} {obra} --pdf-code \"{args.pdf_code}\" --download")
        print(f"  Destino: {destino}")

    # 4) Resumen final.
    print()
    print("=" * 78)
    print(" RESUMEN")
    print("=" * 78)
    print(f"  Contratos:                 {len(contratos)}")
    print(f"  Docs encontrados:          {len(unique_docs)}")
    print(f"    con binario:             {len(docs_con_binario)}")
    print(f"  gra encontrados por PDF:   {len(gra_ids_from_pdf_check)}")
    print()
    print("  Vías exploradas para contrato:")
    print("    VÍA 1: RCG→gra           (ruesma.rcg -> ruesma_rep.gra)")
    print("    VÍA 2: DOG.ctride        (ruesma.dog.ctride = ctr.ide)")
    print("    VÍA 3: CONDOG→dog        (ruesma.condog -> dog)")
    print("    VÍA 4: DOG.conide        (ruesma.dog.conide = ctr.ide)")
    print("    VÍA 5: PFfir.graide→gra  (ruesma.PFfir -> ruesma_rep.gra)")
    print("    VÍA 6: PFfir.dogide→dog  (ruesma.PFfir -> dog)")
    print()
    print("  Comprobación inversa por PDF:")
    print("    gra -> rcg / PFfir / acugra / k_acd / versiones gra.graant")
    print("=" * 78)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
