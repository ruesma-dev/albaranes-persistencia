-- migrations/2026_06_add_contrato_md_paths.sql
-- Markitdown: columnas para el Markdown del contrato (paralelas a las del PDF).
-- Idempotente: seguro de ejecutar varias veces (PostgreSQL).
ALTER TABLE albaran_contratos_merge
    ADD COLUMN IF NOT EXISTS md_sharepoint_relative_path VARCHAR(1024),
    ADD COLUMN IF NOT EXISTS md_sharepoint_web_url       VARCHAR(1024);

-- Caché de contratos (si reutilizas PDFs entre albaranes, reutiliza también el MD):
ALTER TABLE contratos_cache
    ADD COLUMN IF NOT EXISTS md_sharepoint_relative_path VARCHAR(1024),
    ADD COLUMN IF NOT EXISTS md_sharepoint_web_url       VARCHAR(1024);
