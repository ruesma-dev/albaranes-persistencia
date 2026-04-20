# scripts/trace_sigrid_rcg_dual.py
"""Diagnostico RCG por dos lados: contrato vs PDF.

Objetivo:
  LADO A: CIF/NIF + obra -> ctr.ide -> rcg.con = ctr.ide -> gra
  LADO B: nombre/codigo PDF -> gra.ide -> rcg.gra = gra.ide -> con

Sirve para comprobar si ambos caminos llegan al mismo enlace RCG.
No aplica ningun filtro de tipo documental, extension ni cabecera PDF.
Muestra todos los RCG que encuentre.

Ejemplos:
  python scripts/trace_sigrid_rcg_dual.py B86359866 0695 --pdf-name "SUMINISTROS_DE_OBRAS_MOSTOLES.PED1.r__1_.pdf"
  python scripts/trace_sigrid_rcg_dual.py B86359866 0695 --pdf-code "202412170843089860.vmartin"
  python scripts/trace_sigrid_rcg_dual.py B86359866 0695 --pdf-name "SUMINISTROS_DE_OBRAS_MOSTOLES.PED1.r__1_.pdf" --download
  python scripts/trace_sigrid_rcg_dual.py B86359866 0695 --pdf-code "202412170843089860.vmartin" --deep

Variables .env esperadas:
  SIGRID_API_BASE_URL
  SIGRID_API_FUNCTION_KEY
  SIGRID_API_DATABASE      defecto: ruesma
  SIGRID_API_DATABASE_REP  defecto: ruesma_rep
  SIGRID_API_TIMEOUT_S     defecto: 30
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


# ---------------------------------------------------------------------------
# .env
# ---------------------------------------------------------------------------
def load_dotenv_manually(env_path: Path) -> dict[str, str]:
    if not env_path.exists():
        return {}
    result: dict[str, str] = {}
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        result[key] = value
    return result


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ENV_PATH = _PROJECT_ROOT / ".env"
env = load_dotenv_manually(_ENV_PATH)


def get_cfg(name: str) -> str | None:
    return os.environ.get(name) or env.get(name)


base_url = get_cfg("SIGRID_API_BASE_URL")
function_key = get_cfg("SIGRID_API_FUNCTION_KEY")
database = get_cfg("SIGRID_API_DATABASE") or "ruesma"
database_rep = get_cfg("SIGRID_API_DATABASE_REP") or "ruesma_rep"
timeout_s = float(get_cfg("SIGRID_API_TIMEOUT_S") or "30")


# ---------------------------------------------------------------------------
# Args y utilidades
# ---------------------------------------------------------------------------
def normalize_obra_code(raw: str | None) -> str | None:
    if raw is None:
        return None
    c = str(raw).strip()
    if not c or not c.isdigit():
        return None
    if len(c) == 3:
        return "0" + c
    if len(c) == 4:
        return c
    return c


def normalize_cif(raw: str | None) -> str | None:
    if raw is None:
        return None
    c = str(raw).strip().upper()
    c = re.sub(r"[\s\-.]", "", c)
    return c or None


def cif_variants(cif: str) -> list[str]:
    vals: list[str] = []
    c = normalize_cif(cif) or cif
    vals.append(c)
    if c.startswith("ES") and len(c) > 2:
        vals.append(c[2:])
    else:
        vals.append("ES" + c)
    # dedupe preservando orden
    out: list[str] = []
    for v in vals:
        if v and v not in out:
            out.append(v)
    return out


def sanitize_filename(name: str) -> str:
    s = re.sub(r'[\\/:*?"<>|]', "_", str(name))
    s = s.strip().rstrip(".")
    return s or "documento"


def safe_int(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except Exception:
        return None


def csv_ints(values: list[int] | set[int]) -> str:
    nums = sorted({int(v) for v in values if v is not None})
    if not nums:
        return "NULL"
    return ",".join(str(v) for v in nums)


parser = argparse.ArgumentParser(
    description="Traza RCG por dos lados: CIF+obra->contrato y nombre/codigo PDF->gra."
)
parser.add_argument("cif", nargs="?", default="B86359866", help="CIF/NIF proveedor")
parser.add_argument("obra", nargs="?", default="0695", help="Codigo de obra")
parser.add_argument("--pdf-name", dest="pdf_name", default=None, help="Nombre del PDF, por ejemplo xxx.pdf")
parser.add_argument("--pdf-code", dest="pdf_code", default=None, help="Codigo gra.cod, por ejemplo 202412170843089860.vmartin")
parser.add_argument("--download", action="store_true", help="Descarga todos los gra con binario encontrados por ambos lados")
parser.add_argument("--download-side", choices=["both", "contract", "pdf"], default="both", help="Que lado descargar si se usa --download")
parser.add_argument("--deep", action="store_true", help="Busca en que tablas aparece cada concepto ide detectado")
parser.add_argument("--max-rows", type=int, default=500, help="Maximo de filas por consulta")
args = parser.parse_args()

cif = normalize_cif(args.cif)
obra = normalize_obra_code(args.obra)
cif_vars = cif_variants(cif) if cif else []

print("=" * 78)
print(" DIAGNOSTICO SIGRID - CRUCE RCG CONTRATO vs PDF")
print("=" * 78)
print(f"Raiz detectada : {_PROJECT_ROOT}")
print(f"Ruta del .env  : {_ENV_PATH}")
print(f"Existe .env    : {_ENV_PATH.exists()}")
print("-" * 78)
print("Variables detectadas:")
print(f"  SIGRID_API_BASE_URL     = {base_url!r}")
if function_key:
    print(f"  SIGRID_API_FUNCTION_KEY = <presente, len={len(function_key)}, ultimos 4={function_key[-4:]!r}>")
else:
    print(f"  SIGRID_API_FUNCTION_KEY = {function_key!r}")
print(f"  SIGRID_API_DATABASE     = {database!r}")
print(f"  SIGRID_API_DATABASE_REP = {database_rep!r}")
print(f"  SIGRID_API_TIMEOUT_S    = {timeout_s}")
print("-" * 78)
print("Parametros:")
print(f"  cif             = {cif!r}")
print(f"  cif_vars        = {cif_vars!r}")
print(f"  obra            = {obra!r}")
print(f"  --pdf-name      = {args.pdf_name!r}")
print(f"  --pdf-code      = {args.pdf_code!r}")
print(f"  --download      = {args.download}")
print(f"  --download-side = {args.download_side}")
print(f"  --deep          = {args.deep}")
print("-" * 78)
print("IMPORTANTE: no se aplica filtro de tipo documental, extension ni cabecera PDF.")
print("           Se listan todos los enlaces RCG encontrados.")
print("-" * 78)

problems: list[str] = []
if not base_url:
    problems.append("SIGRID_API_BASE_URL vacio o no definido")
elif not base_url.startswith(("http://", "https://")):
    problems.append("SIGRID_API_BASE_URL no empieza por http(s)://")
if not function_key:
    problems.append("SIGRID_API_FUNCTION_KEY vacio o no definido")
if not cif:
    problems.append("CIF invalido")
if not obra:
    problems.append("Codigo de obra invalido")
if problems:
    for p in problems:
        print(f"❌ {p}")
    sys.exit(2)

try:
    import httpx
except ImportError:
    print("❌ Falta httpx. Instala con: pip install httpx")
    sys.exit(4)


# ---------------------------------------------------------------------------
# Helpers API
# ---------------------------------------------------------------------------
def run_query(
    label: str,
    sql: str,
    parameters: list[Any] | None = None,
    max_rows: int | None = None,
    target_database: str | None = None,
) -> dict[str, Any] | None:
    db = target_database or database
    url = f"{base_url.rstrip('/')}/api/sql/read"
    payload = {
        "database": db,
        "sql": sql,
        "parameters": parameters or [],
        "timeout_seconds": int(timeout_s),
        "max_rows": max_rows or args.max_rows,
    }
    headers = {"x-functions-key": function_key, "Content-Type": "application/json"}

    print(f"\n{'─' * 78}")
    print(f"  📡 {label}")
    print(f"{'─' * 78}")
    print(f"  db={db}  max_rows={payload['max_rows']}")
    if parameters:
        print(f"  params={parameters!r}")

    try:
        with httpx.Client(timeout=timeout_s) as c:
            r = c.post(url, json=payload, headers=headers)
    except Exception as exc:
        print(f"  ❌ Error HTTP: {exc!r}")
        return None

    print(f"  HTTP {r.status_code}")
    if r.status_code != 200:
        print(f"  Body: {r.text[:1000]}")
        return None

    try:
        body = r.json()
    except json.JSONDecodeError:
        print("  ❌ JSON invalido")
        print(r.text[:1000])
        return None

    if not body.get("ok"):
        print(f"  ❌ ok=false: {json.dumps(body, ensure_ascii=False)[:1200]}")
        return None

    rc = body.get("row_count", 0)
    tr = body.get("truncated", False)
    print(f"  ✅ {rc} fila(s){' (TRUNCADO)' if tr else ''}")
    return body


def rows_to_dicts(body: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not body or not body.get("rows"):
        return []
    cols = body.get("columns") or []
    return [dict(zip(cols, row)) for row in body["rows"]]


def print_rows(title: str, rows: list[dict[str, Any]], limit: int = 50) -> None:
    print(f"\n{'━' * 78}")
    print(f"  {title}: {len(rows)} fila(s)")
    print(f"{'━' * 78}")
    if not rows:
        print("  Sin resultados.")
        return
    for i, row in enumerate(rows[:limit], start=1):
        print(f"\n  Fila {i}:")
        for k, v in row.items():
            print(f"    {k:<32} = {v!r}")
    if len(rows) > limit:
        print(f"\n  ... {len(rows) - limit} fila(s) mas no mostradas")


def download_gra(gra_ide: int, out_dir: Path, fallback_name: str) -> bool:
    url = f"{base_url.rstrip('/')}/api/documents/read"
    payload = {
        "database": database_rep,
        "schema": "dbo",
        "table": "gra",
        "id_column": "ide",
        "id_value": gra_ide,
        "blob_column": "ima",
        "filename_columns": ["res", "nomori", "nom", "cod"],
        "disposition": "attachment",
    }
    headers = {"x-functions-key": function_key, "Content-Type": "application/json"}
    try:
        with httpx.Client(timeout=120) as c:
            r = c.post(url, json=payload, headers=headers)
    except Exception as exc:
        print(f"    ❌ Error descarga gra.ide={gra_ide}: {exc!r}")
        return False
    if r.status_code != 200:
        print(f"    ❌ HTTP {r.status_code} gra.ide={gra_ide}: {r.text[:500]}")
        return False
    binary = r.content
    if not binary:
        print(f"    ❌ Respuesta vacia gra.ide={gra_ide}")
        return False

    fname = sanitize_filename(r.headers.get("X-Document-Filename", fallback_name))
    if not Path(fname).suffix and binary[:4] == b"%PDF":
        fname += ".pdf"
    out_path = out_dir / fname
    if out_path.exists():
        out_path = out_dir / f"{out_path.stem}_gra{gra_ide}{out_path.suffix}"
    out_path.write_bytes(binary)
    print(f"    ✅ gra.ide={gra_ide} -> {out_path.name} ({len(binary):,} bytes, {r.headers.get('Content-Type', '?')})")
    return True


# ---------------------------------------------------------------------------
# SQL: localizar contrato por CIF + obra
# ---------------------------------------------------------------------------
contract_sql = """
SELECT
    ctr.ide             AS contrato_ide,
    con_ctr.cod         AS codigo_contrato,
    con_ctr.res         AS nombre_contrato,
    con_ctr.fec         AS con_fecha_alta,
    con_ctr.fecbaj      AS con_fecha_baja,
    ctr.fecdoc          AS fecha_contrato,
    ctr.entide          AS proveedor_ide_ctr,
    ctr.entcifpai       AS ctr_cif_pais,
    ctr.entcif          AS ctr_cif,
    ctr.entres          AS ctr_proveedor,
    ctr.entref          AS ctr_referencia_proveedor,
    prv.ide             AS proveedor_ide_prv,
    prv.cif             AS prv_cif,
    prv.raz             AS prv_razon_social,
    con_obr.ide         AS obra_ide,
    con_obr.cod         AS codigo_obra,
    con_obr.res         AS nombre_obra
FROM ctr
JOIN [con] AS con_ctr
    ON con_ctr.ide = ctr.ide
JOIN [con] AS con_obr
    ON con_obr.ide = ctr.obride
LEFT JOIN prv
    ON prv.ide = ctr.entide
WHERE
    con_obr.cod = ?
    AND (
        UPPER(REPLACE(REPLACE(REPLACE(ISNULL(prv.cif, ''), ' ', ''), '-', ''), '.', '')) IN (?, ?)
        OR UPPER(REPLACE(REPLACE(REPLACE(ISNULL(ctr.entcif, ''), ' ', ''), '-', ''), '.', '')) IN (?, ?)
        OR UPPER(REPLACE(REPLACE(REPLACE(ISNULL(ctr.entcifpai, '') + ISNULL(ctr.entcif, ''), ' ', ''), '-', ''), '.', '')) IN (?, ?)
    )
ORDER BY
    ctr.fecdoc DESC,
    ctr.ide DESC;
"""
contract_params = [obra] + cif_vars[:2] + cif_vars[:2] + cif_vars[:2]
body_contracts = run_query("LADO A - localizar contrato por CIF/NIF + obra", contract_sql, contract_params, max_rows=200)
contracts = rows_to_dicts(body_contracts)
print_rows("LADO A - contratos localizados", contracts, limit=50)
contract_ids = {safe_int(r.get("contrato_ide")) for r in contracts if safe_int(r.get("contrato_ide")) is not None}
contract_ids = {int(x) for x in contract_ids if x is not None}


# ---------------------------------------------------------------------------
# LADO A: ctr.ide -> rcg.con
# ---------------------------------------------------------------------------
contract_rcg_rows: list[dict[str, Any]] = []
if contract_ids:
    ides_csv = csv_ints(contract_ids)
    sql_contract_rcg = f"""
SELECT
    'A_CIF_OBRA_TO_CONTRATO_TO_RCG' AS lado,
    r.ide                    AS rcg_ide,
    r.[con]                  AS rcg_con,
    r.gra                    AS rcg_gra,
    r.pos                    AS rcg_pos,
    r.cla                    AS rcg_clase,

    con_rcg.cod              AS rcg_con_cod,
    con_rcg.res              AS rcg_con_res,
    con_rcg.fec              AS rcg_con_fec,
    con_rcg.fecbaj           AS rcg_con_fecbaj,

    ctr.ide                  AS contrato_ide,
    con_ctr.cod              AS codigo_contrato,
    con_ctr.res              AS nombre_contrato,
    ctr.fecdoc               AS fecha_contrato,
    ctr.entcif               AS ctr_cif,
    ctr.entres               AS ctr_proveedor,
    con_obr.cod              AS codigo_obra,
    con_obr.res              AS nombre_obra,

    g.ide                    AS gra_ide,
    g.cod                    AS gra_cod,
    g.res                    AS gra_res,
    g.nom                    AS gra_nom,
    g.nomori                 AS gra_nomori,
    g.fec                    AS gra_fec,
    g.usu                    AS gra_usu,
    g.vin                    AS gra_vin,
    g.gratipide              AS gra_tipide,
    DATALENGTH(g.ima)        AS ima_bytes,
    CASE
        WHEN SUBSTRING(CAST(g.ima AS varbinary(max)), 1, 4) = 0x25504446
        THEN 1 ELSE 0
    END                      AS es_pdf_magic
FROM rcg AS r
LEFT JOIN [con] AS con_rcg
    ON con_rcg.ide = r.[con]
LEFT JOIN ctr
    ON ctr.ide = r.[con]
LEFT JOIN [con] AS con_ctr
    ON con_ctr.ide = ctr.ide
LEFT JOIN [con] AS con_obr
    ON con_obr.ide = ctr.obride
LEFT JOIN {database_rep}.dbo.gra AS g
    ON g.ide = r.gra
WHERE
    r.[con] IN ({ides_csv})
ORDER BY
    r.[con],
    r.pos,
    r.ide;
"""
    body_contract_rcg = run_query("LADO A - RCG donde rcg.con = contrato_ide", sql_contract_rcg, [], max_rows=args.max_rows)
    contract_rcg_rows = rows_to_dicts(body_contract_rcg)
print_rows("LADO A - enlaces RCG del contrato", contract_rcg_rows, limit=100)


# ---------------------------------------------------------------------------
# LADO B: PDF name/code -> gra
# ---------------------------------------------------------------------------
pdf_gra_rows: list[dict[str, Any]] = []
if args.pdf_name or args.pdf_code:
    clauses: list[str] = []
    params: list[Any] = []
    if args.pdf_code:
        clauses.append("g.cod = ?")
        params.append(args.pdf_code)
    if args.pdf_name:
        # exacto + LIKE en columnas habituales. Sin filtro de tipo.
        clauses.append("(g.nom = ? OR g.nomori = ? OR g.res = ? OR g.cod = ? OR g.nom LIKE ? OR g.nomori LIKE ? OR g.res LIKE ? OR g.cod LIKE ?)")
        like = f"%{args.pdf_name}%"
        params.extend([args.pdf_name, args.pdf_name, args.pdf_name, args.pdf_name, like, like, like, like])

    where_pdf = " OR ".join(clauses) if clauses else "1=0"
    sql_pdf_gra = f"""
SELECT TOP (200)
    g.ide                    AS gra_ide,
    g.cod                    AS gra_cod,
    g.res                    AS gra_res,
    g.nom                    AS gra_nom,
    g.nomori                 AS gra_nomori,
    g.tex                    AS gra_tex,
    g.cla                    AS gra_clave,
    g.fec                    AS gra_fec,
    g.usu                    AS gra_usu,
    g.vin                    AS gra_vin,
    g.guid                   AS gra_guid,
    g.gratipide              AS gra_tipide,
    g.graant                 AS gra_version_anterior,
    DATALENGTH(g.ima)        AS ima_bytes,
    CASE
        WHEN SUBSTRING(CAST(g.ima AS varbinary(max)), 1, 4) = 0x25504446
        THEN 1 ELSE 0
    END                      AS es_pdf_magic
FROM {database_rep}.dbo.gra AS g
WHERE
    {where_pdf}
ORDER BY
    g.fec DESC,
    g.ide DESC;
"""
    body_pdf_gra = run_query("LADO B - buscar GRA por nombre/codigo del PDF", sql_pdf_gra, params, max_rows=200)
    pdf_gra_rows = rows_to_dicts(body_pdf_gra)
print_rows("LADO B - gra encontrados por PDF", pdf_gra_rows, limit=100)


# ---------------------------------------------------------------------------
# LADO B: gra.ide -> rcg.gra
# ---------------------------------------------------------------------------
pdf_rcg_rows: list[dict[str, Any]] = []
pdf_gra_ids = {safe_int(r.get("gra_ide")) for r in pdf_gra_rows if safe_int(r.get("gra_ide")) is not None}
pdf_gra_ids = {int(x) for x in pdf_gra_ids if x is not None}

if pdf_gra_ids:
    gra_csv = csv_ints(pdf_gra_ids)
    sql_pdf_rcg = f"""
SELECT
    'B_PDF_TO_GRA_TO_RCG' AS lado,
    r.ide                    AS rcg_ide,
    r.[con]                  AS rcg_con,
    r.gra                    AS rcg_gra,
    r.pos                    AS rcg_pos,
    r.cla                    AS rcg_clase,

    con_rcg.cod              AS rcg_con_cod,
    con_rcg.res              AS rcg_con_res,
    con_rcg.fec              AS rcg_con_fec,
    con_rcg.fecbaj           AS rcg_con_fecbaj,

    CASE
        WHEN ctr.ide IS NOT NULL THEN 'ctr'
        WHEN obr.ide IS NOT NULL THEN 'obr'
        WHEN prv.ide IS NOT NULL THEN 'prv'
        WHEN cli.ide IS NOT NULL THEN 'cli'
        ELSE 'con/otro'
    END                      AS rcg_con_tipo_basico,

    ctr.ide                  AS contrato_ide,
    con_ctr.cod              AS codigo_contrato,
    con_ctr.res              AS nombre_contrato,
    ctr.fecdoc               AS fecha_contrato,
    ctr.entcif               AS ctr_cif,
    ctr.entres               AS ctr_proveedor,
    con_obr.cod              AS codigo_obra,
    con_obr.res              AS nombre_obra,

    g.ide                    AS gra_ide,
    g.cod                    AS gra_cod,
    g.res                    AS gra_res,
    g.nom                    AS gra_nom,
    g.nomori                 AS gra_nomori,
    g.fec                    AS gra_fec,
    g.usu                    AS gra_usu,
    g.vin                    AS gra_vin,
    g.gratipide              AS gra_tipide,
    DATALENGTH(g.ima)        AS ima_bytes,
    CASE
        WHEN SUBSTRING(CAST(g.ima AS varbinary(max)), 1, 4) = 0x25504446
        THEN 1 ELSE 0
    END                      AS es_pdf_magic
FROM rcg AS r
LEFT JOIN [con] AS con_rcg
    ON con_rcg.ide = r.[con]
LEFT JOIN ctr
    ON ctr.ide = r.[con]
LEFT JOIN obr
    ON obr.ide = r.[con]
LEFT JOIN prv
    ON prv.ide = r.[con]
LEFT JOIN cli
    ON cli.ide = r.[con]
LEFT JOIN [con] AS con_ctr
    ON con_ctr.ide = ctr.ide
LEFT JOIN [con] AS con_obr
    ON con_obr.ide = ctr.obride
LEFT JOIN {database_rep}.dbo.gra AS g
    ON g.ide = r.gra
WHERE
    r.gra IN ({gra_csv})
ORDER BY
    r.gra,
    r.[con],
    r.pos,
    r.ide;
"""
    body_pdf_rcg = run_query("LADO B - RCG donde rcg.gra = gra_ide del PDF", sql_pdf_rcg, [], max_rows=args.max_rows)
    pdf_rcg_rows = rows_to_dicts(body_pdf_rcg)
print_rows("LADO B - enlaces RCG del PDF", pdf_rcg_rows, limit=100)


# ---------------------------------------------------------------------------
# Comparativa RCG
# ---------------------------------------------------------------------------
contract_gra_ids = {safe_int(r.get("rcg_gra")) for r in contract_rcg_rows if safe_int(r.get("rcg_gra")) is not None}
contract_gra_ids = {int(x) for x in contract_gra_ids if x is not None}

contract_rcg_con_ids = {safe_int(r.get("rcg_con")) for r in contract_rcg_rows if safe_int(r.get("rcg_con")) is not None}
contract_rcg_con_ids = {int(x) for x in contract_rcg_con_ids if x is not None}

pdf_rcg_con_ids = {safe_int(r.get("rcg_con")) for r in pdf_rcg_rows if safe_int(r.get("rcg_con")) is not None}
pdf_rcg_con_ids = {int(x) for x in pdf_rcg_con_ids if x is not None}

common_gra = contract_gra_ids.intersection(pdf_gra_ids)
common_con = contract_ids.intersection(pdf_rcg_con_ids)

print(f"\n{'=' * 78}")
print(" COMPARATIVA RCG")
print(f"{'=' * 78}")
print(f"  Contrato(s) por CIF+obra                      : {sorted(contract_ids)}")
print(f"  LADO A gra enlazados por rcg.con=contrato     : {sorted(contract_gra_ids)}")
print(f"  LADO B gra encontrados por nombre/codigo PDF  : {sorted(pdf_gra_ids)}")
print(f"  LADO B conceptos enlazados por rcg.gra=PDF    : {sorted(pdf_rcg_con_ids)}")
print(f"  Interseccion de gra entre lado A y lado B     : {sorted(common_gra)}")
print(f"  Interseccion contrato_ide vs rcg.con del PDF  : {sorted(common_con)}")

if pdf_gra_ids and not common_gra and not common_con:
    print("\n  ⚠️  NO COINCIDEN EN RCG.")
    print("     El PDF localizado por nombre/codigo existe en gra, pero su rcg.gra")
    print("     apunta a otro concepto, no al contrato localizado por CIF+obra.")
    if pdf_rcg_rows:
        print("     Conceptos a los que apunta el PDF por rcg:")
        for row in pdf_rcg_rows:
            print(
                f"       gra={row.get('rcg_gra')} -> rcg.con={row.get('rcg_con')} "
                f"cod={row.get('rcg_con_cod')!r} res={row.get('rcg_con_res')!r}"
            )
    else:
        print("     Ademas, el PDF no tiene filas en rcg.gra.")
elif common_gra or common_con:
    print("\n  ✅ Hay coincidencia entre los dos lados en RCG.")
else:
    print("\n  ℹ️  No se pudo comparar: falta el lado A o el lado B.")

# Resumen compacto por lado
print("\n  Resumen conteos:")
print(f"    contratos encontrados        : {len(contracts)}")
print(f"    filas rcg lado A contrato    : {len(contract_rcg_rows)}")
print(f"    filas gra lado B PDF         : {len(pdf_gra_rows)}")
print(f"    filas rcg lado B PDF         : {len(pdf_rcg_rows)}")
if contract_rcg_rows:
    print(f"    lado A por usuario gra       : {dict(Counter(r.get('gra_usu') for r in contract_rcg_rows))}")
if pdf_rcg_rows:
    print(f"    lado B por concepto basico   : {dict(Counter(r.get('rcg_con_tipo_basico') for r in pdf_rcg_rows))}")


# ---------------------------------------------------------------------------
# --deep: detectar en que tablas aparece cada concepto ide
# ---------------------------------------------------------------------------
def deep_tables_for_ide(target_ide: int, label: str) -> list[dict[str, Any]]:
    sql = """
DECLARE @target_ide int = ?;
DECLARE @sql nvarchar(max) = N'';

SELECT @sql = @sql +
    CASE WHEN @sql = N'' THEN N'' ELSE N' UNION ALL ' END +
    N'SELECT ' + QUOTENAME(s.name + N'.' + t.name, '''') + N' AS tabla, COUNT_BIG(*) AS filas ' +
    N'FROM ' + QUOTENAME(s.name) + N'.' + QUOTENAME(t.name) + N' WITH (NOLOCK) ' +
    N'WHERE [ide] = @target_ide HAVING COUNT_BIG(*) > 0'
FROM sys.tables AS t
JOIN sys.schemas AS s
    ON s.schema_id = t.schema_id
JOIN sys.columns AS c
    ON c.object_id = t.object_id
JOIN sys.types AS ty
    ON ty.user_type_id = c.user_type_id
WHERE
    t.is_ms_shipped = 0
    AND c.name = N'ide'
    AND ty.name IN (N'int', N'bigint', N'numeric', N'decimal')
ORDER BY
    s.name,
    t.name;

IF @sql = N''
BEGIN
    SELECT CAST(NULL AS nvarchar(256)) AS tabla, CAST(0 AS bigint) AS filas WHERE 1 = 0;
END
ELSE
BEGIN
    EXEC sp_executesql @sql, N'@target_ide int', @target_ide = @target_ide;
END
"""
    body = run_query(f"DEEP - tablas donde ide = {target_ide} ({label})", sql, [target_ide], max_rows=500)
    rows = rows_to_dicts(body)
    print_rows(f"DEEP - tablas con ide={target_ide} ({label})", rows, limit=100)
    return rows

if args.deep:
    ids_to_probe: list[tuple[int, str]] = []
    for cid in sorted(contract_ids):
        ids_to_probe.append((cid, "contrato desde CIF+obra"))
    for conid in sorted(pdf_rcg_con_ids):
        ids_to_probe.append((conid, "concepto al que apunta el PDF por rcg"))

    seen_ids: set[int] = set()
    for target_ide, label in ids_to_probe:
        if target_ide in seen_ids:
            continue
        seen_ids.add(target_ide)
        deep_tables_for_ide(target_ide, label)


# ---------------------------------------------------------------------------
# Descargar gra de ambos lados si se pide
# ---------------------------------------------------------------------------
if args.download:
    download_items: dict[int, str] = {}

    if args.download_side in ("both", "contract"):
        for row in contract_rcg_rows:
            gid = safe_int(row.get("gra_ide") or row.get("rcg_gra"))
            if not gid:
                continue
            name = row.get("gra_res") or row.get("gra_nomori") or row.get("gra_nom") or row.get("gra_cod") or f"gra_{gid}"
            download_items[gid] = str(name)

    if args.download_side in ("both", "pdf"):
        for row in pdf_gra_rows:
            gid = safe_int(row.get("gra_ide"))
            if not gid:
                continue
            name = row.get("gra_res") or row.get("gra_nomori") or row.get("gra_nom") or row.get("gra_cod") or f"gra_{gid}"
            download_items[gid] = str(name)

    folder_name = sanitize_filename(f"rcg_dual_{obra}_{cif}")
    out_dir = Path.home() / "Downloads" / "sigrid_docs" / folder_name
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n{'━' * 78}")
    print(f"  ⬇️  DESCARGANDO {len(download_items)} gra(s)")
    print(f"  Lado descarga: {args.download_side}")
    print(f"  Carpeta      : {out_dir}")
    print(f"{'━' * 78}")

    ok = 0
    fail = 0
    for idx, (gid, fallback) in enumerate(sorted(download_items.items()), start=1):
        print(f"\n  [{idx}/{len(download_items)}] gra.ide={gid} fallback={fallback!r}")
        if download_gra(gid, out_dir, fallback):
            ok += 1
        else:
            fail += 1
    print(f"\n  Resultado descarga: {ok} OK, {fail} fallidos")
    print(f"  explorer \"{out_dir}\"")

print(f"\n{'=' * 78}")
print(" FIN")
print(f"{'=' * 78}")
