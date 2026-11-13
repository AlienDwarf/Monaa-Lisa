import { promisify } from 'node:util';
import { gzip as gzipCb } from 'node:zlib';

const gzip = promisify(gzipCb);

/** Fertig serialisierte Antwort: einmal JSON + einmal gzip, beliebig oft ausgeliefert. */
export interface CachedPayload {
  json: Buffer;
  gz: Buffer;
  createdAt: number;
}

/**
 * Cache für GET /papers/points.
 *
 * Die Punktwolke einer großen Kategorie ist zig MB groß. Ohne Cache würde jede
 * Anfrage die Abfrage erneut ausführen und das JSON neu bauen + komprimieren
 * (blockiert den Event-Loop, viel RAM). Die Daten ändern sich nur beim
 * Pipeline-Update, deshalb:
 * - fertige Bytes (JSON + gzip) pro (Kategorie, Limit) im Speicher halten
 * - LRU mit Byte-Budget, Einträge verfallen nach `ttlMs`
 * - gleichzeitige Anfragen für denselben Schlüssel teilen sich EINE Berechnung
 */
export class PointsCache {
  private readonly entries = new Map<string, CachedPayload>();
  private readonly inflight = new Map<string, Promise<CachedPayload>>();
  private bytes = 0;

  constructor(
    private readonly maxBytes: number,
    private readonly ttlMs: number,
  ) {}

  async get(
    key: string,
    build: () => Promise<unknown>,
  ): Promise<CachedPayload> {
    const hit = this.entries.get(key);
    if (hit && Date.now() - hit.createdAt < this.ttlMs) {
      // LRU: ans Ende verschieben
      this.entries.delete(key);
      this.entries.set(key, hit);
      return hit;
    }
    if (hit) this.remove(key);

    const running = this.inflight.get(key);
    if (running) return running;

    const p = (async () => {
      const data = await build();
      const json = Buffer.from(JSON.stringify(data));
      const gz = await gzip(json, { level: 6 });
      const entry: CachedPayload = { json, gz, createdAt: Date.now() };
      this.store(key, entry);
      return entry;
    })();
    this.inflight.set(key, p);
    try {
      return await p;
    } finally {
      this.inflight.delete(key);
    }
  }

  clear() {
    this.entries.clear();
    this.bytes = 0;
  }

  private store(key: string, entry: CachedPayload) {
    const size = entry.json.length + entry.gz.length;
    // Zu groß für den Cache → nur ausliefern, nicht behalten
    if (size > this.maxBytes) return;
    this.entries.set(key, entry);
    this.bytes += size;
    for (const k of this.entries.keys()) {
      if (this.bytes <= this.maxBytes) break;
      this.remove(k);
    }
  }

  private remove(key: string) {
    const e = this.entries.get(key);
    if (!e) return;
    this.entries.delete(key);
    this.bytes -= e.json.length + e.gz.length;
  }
}
