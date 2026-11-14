-- Repariert doppelt kodierte UMAP-Koordinaten.
-- Die inkrementelle Python-Pipeline hat `tsne` bisher als JSON-String
-- ("{\"x\": 1.2, ...}") statt als JSON-Objekt gespeichert (Ursache in
-- Database/db.py behoben). Hier werden diese Zeilen einmalig in echte
-- Objekte umgewandelt. Zeilen mit gültigen Objekten bleiben unverändert.
UPDATE "paper"
   SET "tsne" = ("tsne"::jsonb #>> '{}')::jsonb
 WHERE "tsne" IS NOT NULL
   AND jsonb_typeof("tsne"::jsonb) = 'string';
