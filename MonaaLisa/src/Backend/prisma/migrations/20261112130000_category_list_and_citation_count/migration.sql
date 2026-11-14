-- K2: Kategorien als Array + vorberechnete Zitationszahl.
--
-- Vorher: Filter per LIKE '%cs.CL%' (trifft auch physics.class-ph) und für
-- jede Sortierung nach Zitationen ein COUNT über paper_citation.
-- Nachher: exakter Array-Match über GIN-Index und eine indizierte Spalte.
--
-- Reihenfolge bewusst: erst Spalten (bei konstantem Default nur Metadaten,
-- daher sofort), dann EIN Update über alle Zeilen, dann Indizes bauen –
-- Indizes nach dem Befüllen anzulegen ist deutlich schneller.
-- Auf dem vollen Datensatz dauert das einige Minuten (einmalig).

-- DropIndex: B-Tree über den ganzen Kategorie-String + andere Kennzahl,
-- wurde von keiner Abfrage genutzt
DROP INDEX IF EXISTS "paper_categories_non_arxiv_citation_count_idx";

-- AlterTable
ALTER TABLE "paper" ADD COLUMN     "arxiv_citation_count" INTEGER NOT NULL DEFAULT 0,
ADD COLUMN     "category_list" TEXT[] DEFAULT ARRAY[]::TEXT[],
ADD COLUMN     "primary_category" TEXT;

-- Backfill. Kategorien können durch Leerzeichen oder Kommas getrennt sein.
WITH cc AS (
    SELECT belonging_paper_entry_id AS entry_id, COUNT(*)::int AS cnt
      FROM "paper_citation"
     GROUP BY belonging_paper_entry_id
),
src AS (
    SELECT p.id,
           NULLIF(btrim(regexp_replace(COALESCE(p.categories, ''), '[\s,]+', ' ', 'g')), '') AS cats,
           COALESCE(cc.cnt, 0) AS cnt
      FROM "paper" p
      LEFT JOIN cc ON cc.entry_id = p.entry_id
)
UPDATE "paper" p
   SET "category_list"        = CASE WHEN src.cats IS NULL THEN ARRAY[]::TEXT[]
                                     ELSE string_to_array(src.cats, ' ') END,
       "primary_category"     = CASE WHEN src.cats IS NULL THEN NULL
                                     ELSE split_part(src.cats, ' ', 1) END,
       "arxiv_citation_count" = src.cnt
  FROM src
 WHERE src.id = p.id;

-- CreateIndex
CREATE INDEX "paper_category_list_idx" ON "paper" USING GIN ("category_list");

-- CreateIndex
CREATE INDEX "paper_arxiv_citation_count_idx" ON "paper"("arxiv_citation_count" DESC);
