-- H4: Merkt sich, ob der Erstimport des Datensatzes vollständig durchlief.
-- Vorher galt "paper-Tabelle nicht leer" als "Import fertig" – ein
-- abgebrochener Import blieb so dauerhaft unvollständig.

-- CreateTable
CREATE TABLE "app_state" (
    "key" TEXT NOT NULL,
    "value" TEXT NOT NULL,
    "updated_at" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "app_state_pkey" PRIMARY KEY ("key")
);

-- Bestehende Installationen: Ist bereits ein Datensatz importiert, gilt der
-- Import als abgeschlossen (bisheriges Verhalten) – sonst würde beim nächsten
-- Start der komplette Datensatz erneut geladen und importiert.
INSERT INTO "app_state" ("key", "value")
SELECT 'dataset_import', 'complete'
 WHERE EXISTS (SELECT 1 FROM "paper" LIMIT 1);
