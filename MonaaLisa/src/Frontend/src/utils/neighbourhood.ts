/**
 * Gemeinsamer Cache für die Zitations-Nachbarschaft eines Papers.
 *
 * PaperDetailGraph und PaperSidebar zeigen dieselben Daten. Statt dass
 * beide unabhängig citations/references und /papers/batch für ALLE
 * Nachbar-IDs laden, teilen sie sich hier:
 *   - die ID-Listen pro Paper (ein Request-Paar pro entry_id), und
 *   - einen begrenzten Cache der per /papers/batch geladenen Paper.
 * Details werden nur für die IDs geladen, die wirklich angezeigt werden,
 * in 500er-Batches mit höchstens BATCH_CONCURRENCY parallelen Requests.
 */

export interface NeighbourIds {
    /** Paper, die dieses Paper zitiert (dedupliziert, Server-Reihenfolge). */
    citedIds: string[];
    /** Paper, die dieses Paper zitieren (dedupliziert, Server-Reihenfolge). */
    referencedIds: string[];
}

/** Schlanke Kopie eines Papers aus POST /papers/batch. */
export interface RawNeighbourPaper {
    id?: number;
    entry_id: string;
    title: string;
    authors: string;
    abstract: string;
    published: string | null;
    categories: string | null;
    url: string | null;
    tsne: unknown;
    cluster: string | null;
    non_arxiv_citation_count: number;
    non_arxiv_reference_count: number;
    citations: unknown[];
    references: unknown[];
}

const BATCH_SIZE = 500;
const BATCH_CONCURRENCY = 3;
const MAX_ID_ENTRIES = 30;
const MAX_PAPER_ENTRIES = 5000;

function trimSlash(url: string): string {
    return url.endsWith("/") ? url.slice(0, -1) : url;
}

// ─── ID-Listen ──────────────────────────────────────────────────────────
const idCache = new Map<string, Promise<NeighbourIds>>();

function abortable<T>(p: Promise<T>, signal?: AbortSignal): Promise<T> {
    if (!signal) return p;
    if (signal.aborted)
        return Promise.reject(new DOMException("Aborted", "AbortError"));
    return new Promise<T>((resolve, reject) => {
        const onAbort = () =>
            reject(new DOMException("Aborted", "AbortError"));
        signal.addEventListener("abort", onAbort, { once: true });
        p.then(
            (v) => {
                signal.removeEventListener("abort", onAbort);
                resolve(v);
            },
            (e) => {
                signal.removeEventListener("abort", onAbort);
                reject(e);
            },
        );
    });
}

/**
 * Lädt die IDs der zitierten und zitierenden Paper (geteilt + gecacht).
 * Der Request selbst wird nicht abgebrochen (ein anderer Konsument kann
 * ihn noch brauchen); `signal` bricht nur das Warten ab.
 */
export function fetchNeighbourIds(
    apiBaseUrl: string,
    entryId: string,
    signal?: AbortSignal,
): Promise<NeighbourIds> {
    const base = trimSlash(apiBaseUrl);
    const key = `${base}|${entryId}`;
    let p = idCache.get(key);
    if (p) {
        // LRU: ans Ende verschieben
        idCache.delete(key);
        idCache.set(key, p);
    } else {
        p = (async () => {
            const enc = encodeURIComponent(entryId);
            const [citRes, refRes] = await Promise.all([
                fetch(`${base}/paper-citations/paper/${enc}`),
                fetch(`${base}/paper-references/paper/${enc}`),
            ]);
            const citations: { cited_paper_entry_id: string }[] = citRes.ok
                ? await citRes.json()
                : [];
            const references: { referenced_paper_entry_id: string }[] =
                refRes.ok ? await refRes.json() : [];
            return {
                citedIds: dedupe(citations.map((c) => c.cited_paper_entry_id)),
                referencedIds: dedupe(
                    references.map((r) => r.referenced_paper_entry_id),
                ),
            };
        })();
        idCache.set(key, p);
        // Fehler nicht cachen
        p.catch(() => {
            if (idCache.get(key) === p) idCache.delete(key);
        });
        while (idCache.size > MAX_ID_ENTRIES) {
            const oldest = idCache.keys().next().value as string;
            idCache.delete(oldest);
        }
    }
    return abortable(p, signal);
}

function dedupe(ids: string[]): string[] {
    const seen = new Set<string>();
    const out: string[] = [];
    for (const id of ids) {
        if (!id || seen.has(id)) continue;
        seen.add(id);
        out.push(id);
    }
    return out;
}

// ─── Paper-Details (POST /papers/batch) ─────────────────────────────────
/** entry_id → Paper, oder null = vom Backend nicht gefunden. */
const paperCache = new Map<string, RawNeighbourPaper | null>();
/** entry_id → laufender Batch-Request, der diese ID enthält. */
const inflight = new Map<string, Promise<void>>();

function cacheSet(id: string, p: RawNeighbourPaper | null) {
    paperCache.delete(id);
    paperCache.set(id, p);
    while (paperCache.size > MAX_PAPER_ENTRIES) {
        const oldest = paperCache.keys().next().value as string;
        paperCache.delete(oldest);
    }
}

function slim(p: any): RawNeighbourPaper {
    return {
        id: p.id,
        entry_id: p.entry_id,
        title: p.title ?? p.entry_id,
        authors: Array.isArray(p.authors)
            ? p.authors.join(", ")
            : (p.authors ?? ""),
        abstract: p.abstract ?? "",
        published: p.published ?? null,
        categories: p.categories ?? null,
        url: p.url ?? null,
        tsne: p.tsne ?? null,
        cluster: p.cluster ?? null,
        non_arxiv_citation_count: Number(p.non_arxiv_citation_count ?? 0),
        non_arxiv_reference_count: Number(p.non_arxiv_reference_count ?? 0),
        citations: Array.isArray(p.citations) ? p.citations : [],
        references: Array.isArray(p.references) ? p.references : [],
    };
}

// Globales Limit paralleler Batch-Requests
let active = 0;
const waiters: (() => void)[] = [];
async function acquire(): Promise<void> {
    if (active < BATCH_CONCURRENCY) {
        active++;
        return;
    }
    await new Promise<void>((resolve) => waiters.push(resolve));
    active++;
}
function release() {
    active--;
    const next = waiters.shift();
    if (next) next();
}

async function runBatch(
    base: string,
    ids: string[],
    signal?: AbortSignal,
): Promise<void> {
    await acquire();
    try {
        if (signal?.aborted) throw new DOMException("Aborted", "AbortError");
        const res = await fetch(`${base}/papers/batch`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ entryIds: ids }),
            signal,
        });
        if (!res.ok) throw new Error(`Backend ${res.status}`);
        const rows: any[] = await res.json();
        const found = new Set<string>();
        for (const row of rows) {
            if (!row?.entry_id) continue;
            found.add(row.entry_id);
            cacheSet(row.entry_id, slim(row));
        }
        for (const id of ids) if (!found.has(id)) cacheSet(id, null);
    } finally {
        release();
    }
}

/**
 * Lädt die Paper zu `ids` (Cache + laufende Requests werden
 * wiederverwendet). Liefert entry_id → Paper für alle gefundenen IDs.
 * Fehlgeschlagene Batches werden ausgelassen (IDs fehlen dann im Ergebnis).
 */
export async function fetchNeighbourPapers(
    apiBaseUrl: string,
    ids: string[],
    signal?: AbortSignal,
): Promise<Map<string, RawNeighbourPaper>> {
    const base = trimSlash(apiBaseUrl);

    const collect = () => {
        const out = new Map<string, RawNeighbourPaper>();
        const missing: string[] = [];
        for (const id of ids) {
            if (paperCache.has(id)) {
                const p = paperCache.get(id);
                if (p) out.set(id, p);
            } else missing.push(id);
        }
        return { out, missing };
    };

    // Zwei Durchläufe: erst auf fremde laufende Requests warten, danach
    // alles, was (z. B. wegen Abbruch des anderen) noch fehlt, selbst laden.
    for (let pass = 0; pass < 2; pass++) {
        const { missing } = collect();
        if (!missing.length) break;

        const waits = new Set<Promise<void>>();
        const toFetch: string[] = [];
        for (const id of missing) {
            const p = pass === 0 ? inflight.get(id) : undefined;
            if (p) waits.add(p);
            else if (!inflight.has(id) || pass > 0) toFetch.push(id);
        }

        for (let i = 0; i < toFetch.length; i += BATCH_SIZE) {
            const chunk = toFetch.slice(i, i + BATCH_SIZE);
            const p = runBatch(base, chunk, signal);
            const settled = p.catch(() => {});
            for (const id of chunk) inflight.set(id, settled);
            settled.then(() => {
                for (const id of chunk)
                    if (inflight.get(id) === settled) inflight.delete(id);
            });
            waits.add(settled);
        }

        await abortable(Promise.all(waits), signal);
        if (pass === 0 && toFetch.length === missing.length) break;
    }

    if (signal?.aborted) throw new DOMException("Aborted", "AbortError");
    return collect().out;
}

/** Bereits gecachtes Paper (ohne Request), sonst undefined. */
export function getCachedNeighbourPaper(
    id: string,
): RawNeighbourPaper | null | undefined {
    return paperCache.get(id);
}
