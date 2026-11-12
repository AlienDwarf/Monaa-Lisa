/**
 * Gemeinsame Helfer zum Laden und Normalisieren von Papern aus dem Backend.
 */
import type { ApiPaper, CategoryPointsResponse, Paper } from "$lib/types/paper";

/** Liest `tsne` als Objekt oder (doppelt kodierten) JSON-String. */
export function parseTsne(tsne: unknown): [number, number] {
    if (!tsne) return [0, 0];
    let obj: any = tsne;
    if (typeof obj === "string") {
        try {
            obj = JSON.parse(obj);
        } catch {
            return [0, 0];
        }
    }
    if (
        obj &&
        typeof obj === "object" &&
        Number.isFinite(obj.x) &&
        Number.isFinite(obj.y)
    ) {
        return [obj.x, obj.y];
    }
    return [0, 0];
}

export function formatAuthors(a: ApiPaper["authors"]): string {
    if (!a) return "";
    return Array.isArray(a) ? a.join(", ") : a;
}

export function deriveStringArray(value: unknown): string[] {
    if (!value) return [];
    if (Array.isArray(value)) {
        return value
            .map((item) => {
                if (typeof item === "string") return item;
                if (typeof item === "number") return item.toString();
                if (item && typeof item === "object" && "entry_id" in item)
                    return (item as any).entry_id ?? "";
                return "";
            })
            .filter(Boolean);
    }
    if (typeof value === "string")
        return value
            .split(",")
            .map((s) => s.trim())
            .filter(Boolean);
    return [];
}

/** Wandelt ein volles Paper-Objekt aus /papers bzw. /papers/:id um. */
export function normalizePaper(raw: ApiPaper, index: number): Paper | null {
    if (!raw?.entry_id || !raw?.title) return null;
    const [t1, t2] = parseTsne(raw.tsne);
    return {
        id: Number(raw.id ?? index),
        entry_id: raw.entry_id,
        title: raw.title,
        authors: formatAuthors(raw.authors),
        abstract: raw.abstract ?? "",
        published: raw.published ?? null,
        categories: raw.categories ?? null,
        url: raw.url ?? null,
        citations: deriveStringArray(raw.citations),
        references: deriveStringArray(raw.references),
        non_arxiv_citation_count: raw.non_arxiv_citation_count ?? 0,
        non_arxiv_reference_count: raw.non_arxiv_reference_count ?? 0,
        tsne1: t1,
        tsne2: t2,
        cluster: raw.categories ?? "U",
        detailsLoaded: true,
    };
}

/**
 * Lädt die Punktwolke einer Kategorie (GET /papers/points).
 * `limit` <= 0 bzw. undefined = alle Paper der Kategorie.
 * Die gelieferten Paper haben nur ID, Koordinaten und Zitationszahlen
 * (`detailsLoaded: false`); Details über `fetchPaperDetails` nachladen.
 */
export async function fetchCategoryPoints(
    apiBaseUrl: string,
    category: string,
    limit: number | undefined,
    signal?: AbortSignal,
): Promise<Paper[]> {
    const params = new URLSearchParams({ category });
    if (limit && limit > 0) params.set("limit", String(limit));
    const res = await fetch(
        `${trimSlash(apiBaseUrl)}/papers/points?${params.toString()}`,
        { signal },
    );
    if (!res.ok) throw new Error(`Backend ${res.status}`);
    const data = (await res.json()) as CategoryPointsResponse;

    const n = data.ids?.length ?? 0;
    const papers: Paper[] = new Array(n);
    for (let i = 0; i < n; i++) {
        papers[i] = {
            id: i,
            entry_id: data.ids[i],
            title: "",
            authors: "",
            abstract: "",
            published: null,
            categories: category,
            url: null,
            citations: [],
            references: [],
            non_arxiv_citation_count: data.nc[i] ?? 0,
            non_arxiv_reference_count: 0,
            tsne1: data.x[i],
            tsne2: data.y[i],
            cluster: category,
            arxiv_citation_count: data.cc[i] ?? 0,
            detailsLoaded: false,
        };
    }
    return papers;
}

/**
 * IDs aller Paper einer Kategorie, deren Autorenfeld `author` enthält
 * (GET /papers/points/ids). Bei Fehlern leere Liste.
 */
export async function fetchAuthorPaperIds(
    apiBaseUrl: string,
    category: string,
    author: string,
    signal?: AbortSignal,
): Promise<string[]> {
    try {
        const params = new URLSearchParams({ category, author });
        const res = await fetch(
            `${trimSlash(apiBaseUrl)}/papers/points/ids?${params.toString()}`,
            { signal },
        );
        if (!res.ok) return [];
        const data = await res.json();
        return Array.isArray(data?.ids) ? data.ids : [];
    } catch {
        return [];
    }
}

/** Lädt das volle Paper inkl. citations/references (GET /papers/:id). */
export async function fetchPaperDetails(
    apiBaseUrl: string,
    entryId: string,
    signal?: AbortSignal,
): Promise<Paper | null> {
    const res = await fetch(
        `${trimSlash(apiBaseUrl)}/papers/${encodeURIComponent(entryId)}`,
        { signal },
    );
    if (!res.ok) return null;
    return normalizePaper((await res.json()) as ApiPaper, 0);
}

/**
 * Ergänzt ein Punkt-Paper um seine Details. Behält die Koordinaten des
 * Punkts, falls die Details keine gültigen liefern.
 */
export function mergeDetails(point: Paper, details: Paper): Paper {
    const hasCoords = details.tsne1 !== 0 || details.tsne2 !== 0;
    return {
        ...details,
        id: point.id,
        tsne1: hasCoords ? details.tsne1 : point.tsne1,
        tsne2: hasCoords ? details.tsne2 : point.tsne2,
        arxiv_citation_count: details.citations.length,
        detailsLoaded: true,
    };
}

function trimSlash(url: string): string {
    return url.endsWith("/") ? url.slice(0, -1) : url;
}
