-- Performance für >2 Mio. Paper.
--
-- 1. point_x / point_y: UMAP-Koordinaten als echte Spalten, automatisch aus
--    `tsne` berechnet (GENERATED ... STORED). Niemand muss sie schreiben;
--    jedes INSERT/UPDATE von `tsne` hält sie aktuell. Vorher wurde für jeden
--    Kartenpunkt das JSON geparst und die ganze (breite) Zeile inkl.
--    Abstract gelesen.
--
-- 2. paper_points_prefix_idx: deckt GET /papers/points?category=cs. komplett
--    ab (Index-Only-Scan, kein Zugriff auf die Tabelle). text_pattern_ops,
--    damit LIKE 'cs.%' den Index trotz Nicht-C-Collation nutzen kann.
--
-- 3. paper_primary_category_idx: Präfix-Filter in GET /papers (findMany).
--
-- 4. paper_published_id_idx: stabile Sortierung published DESC, id DESC
--    (vorher ohne Tiebreaker → Paper doppelt/fehlend beim Blättern).
--
-- 5. uncaught_paper_due_idx: Retry-Queue der Python-Pipeline
--    (retry_count < max_retries, sortiert/paginiert nach id).
--
-- ACHTUNG: Das Hinzufügen der STORED-Spalten schreibt die Tabelle einmal neu
-- (bei 2 Mio. Zeilen einige Minuten, braucht kurzzeitig doppelten
-- Plattenplatz für `paper`). Einmalig.

-- AlterTable
ALTER TABLE "paper"
  ADD COLUMN "point_x" REAL GENERATED ALWAYS AS (
    CASE WHEN jsonb_typeof("tsne" -> 'x') = 'number'
         THEN ("tsne" ->> 'x')::real END
  ) STORED,
  ADD COLUMN "point_y" REAL GENERATED ALWAYS AS (
    CASE WHEN jsonb_typeof("tsne" -> 'y') = 'number'
         THEN ("tsne" ->> 'y')::real END
  ) STORED;

-- CreateIndex
CREATE INDEX "paper_points_prefix_idx" ON "paper"
  ("primary_category" text_pattern_ops, "arxiv_citation_count" DESC, "entry_id")
  INCLUDE ("point_x", "point_y", "non_arxiv_citation_count")
  WHERE "point_x" IS NOT NULL AND "point_y" IS NOT NULL;

-- CreateIndex
CREATE INDEX "paper_primary_category_idx" ON "paper" ("primary_category" text_pattern_ops);

-- CreateIndex
CREATE INDEX "paper_published_id_idx" ON "paper" ("published" DESC, "id" DESC);

-- DropIndex: durch paper_published_id_idx abgedeckt
DROP INDEX IF EXISTS "paper_published_idx";

-- CreateIndex
CREATE INDEX IF NOT EXISTS "uncaught_paper_retry_id_idx" ON "uncaught_paper" ("id")
  WHERE "retry_count" < "max_retries";

-- Statistiken für den Planer nach dem Umbau aktualisieren
ANALYZE "paper";
