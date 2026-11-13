/**
 * Raw paper object as returned by the NestJS backend API.
 */
export interface ApiPaper {
    id?: number;
    entry_id: string;
    title: string;
    authors?: string | string[];
    abstract?: string | null;
    categories?: string | null;
    published?: string | null;
    updated?: string | null;
    doi?: string | null;
    journal_ref?: string | null;
    license?: string | null;
    url?: string | null;
    s2_id?: string | null;
    non_arxiv_citation_count?: number | null;
    non_arxiv_reference_count?: number | null;
    tsne?: unknown;
    related_arxiv_ids?: unknown;
    citations?: unknown;
    references?: unknown;
    cluster?: string | null;
}

/**
 * Normalised paper used internally by the frontend components.
 */
export interface Paper {
    id: number;
    entry_id: string;
    title: string;
    authors: string;
    abstract: string;
    published: string | null;
    categories: string | null;
    url: string | null;
    citations: string[];
    references: string[];
    non_arxiv_citation_count: number;
    non_arxiv_reference_count: number;
    tsne1: number;
    tsne2: number;
    cluster: string;
    /**
     * arXiv-interne Zitationszahl. Bei Papern aus GET /papers/points gesetzt,
     * deren `citations`-Liste noch leer ist (Details noch nicht geladen).
     */
    arxiv_citation_count?: number;
    /**
     * false = nur Punktdaten (ID, Koordinaten, Zitationszahlen) vorhanden;
     * Titel, Autoren, Abstract usw. müssen über /papers/:id nachgeladen werden.
     */
    detailsLoaded?: boolean;
}

/**
 * Spaltenorientierte Antwort von GET /papers/points.
 */
export interface CategoryPointsResponse {
    category: string;
    count: number;
    ids: string[];
    x: number[];
    y: number[];
    cc: number[];
    nc: number[];
}

/**
 * Spaltenorientierte Punktwolke im Frontend (aus CategoryPointsResponse).
 * Index i beschreibt einen Punkt: ids[i], x[i], y[i], cc[i] (arXiv-
 * Zitationen), nc[i] (Nicht-arXiv-Zitationen).
 */
export interface CategoryPoints {
    category: string;
    count: number;
    ids: string[];
    x: Float32Array;
    y: Float32Array;
    cc: Uint32Array;
    nc: Uint32Array;
}

/**
 * A fetched paper from the neighbourhood API (used in PaperSidebar).
 */
export interface NeighbourPaper {
    entry_id: string;
    title: string;
    authors: string;
    abstract: string;
    published: string | null;
    categories: string | null;
    non_arxiv_citation_count: number;
    non_arxiv_reference_count: number;
}

/**
 * A paper browsing-history session stored in localStorage.
 */
export interface PaperSession {
    id: string;
    mainPaper: { entry_id: string; title: string; authors: string };
    startedAt: number;
}

/**
 * Paginated response wrapper returned by GET /papers.
 */
export interface PapersResponse {
    items: ApiPaper[];
    total: number;
}

/**
 * Cluster node for hierarchical category navigation.
 */
export interface ClusterNode {
    id: string;
    name: string;
    count: number;
}

/**
 * Navigation state for the graph views.
 */
export type ViewState =
    | { level: "top" }
    | { level: "sub"; parentName: string; parentId: string }
    | {
          level: "papers";
          categoryId: string;
          categoryName: string;
          parentName: string;
      }
    | {
          level: "detail";
          paper: Paper;
          categoryId: string;
          categoryName: string;
          parentName: string;
          /** arXiv citation count (fetched after load, may be undefined) */
          arxivCitationCount?: number;
          /** arXiv reference count (fetched after load, may be undefined) */
          arxivReferenceCount?: number;
      };
