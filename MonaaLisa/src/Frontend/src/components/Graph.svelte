<!--
  Graph.svelte
  Das rendert den Hauptgraphen mit Sigma.js
  Man sieht eine festgelegte Anzahl von Paper angeordnet nach ihrer thematischen Ähnlichkeit (durch UMAP dargestellt)
  Jeder Datenpunkt ist ein Paper. Das Hovern zeigt einen Tooltipp mit details zum paper
-->
<script lang="ts">
    import { createEventDispatcher, onMount } from "svelte";
    import { env as publicEnv } from "$env/dynamic/public";
    import GraphLib from "graphology";
    import Sigma from "sigma";
    import type { CategoryPoints, Paper } from "$lib/types/paper";
    import {
        fetchAuthorPaperIds,
        fetchPaperDetails,
        mergeDetails,
        pointToPaper,
    } from "../utils/paperApi";

    /** Spaltenorientierte Punktwolke der Kategorie (aus GET /papers/points). */
    export let points: CategoryPoints;
    export let selectedPaperId: string | null = null;
    export let categoryColor: string = "#4a9eff";
    /** Author name to highlight in the graph. Empty string = no highlight. */
    export let authorHighlight: string = "";
    /** API base URL for fetching papers by author */
    export let apiBaseUrl: string =
        publicEnv.PUBLIC_API_BASE_URL || "http://localhost:3000";
    /** Current category ID for scoping author search */
    export let categoryId: string = "";
    // Return value of searched papers
    export let searchHighlightPapers: Paper[] = [];

    /** Backend verlangt für Text-/Autorensuche mindestens 3 Zeichen. */
    const MIN_QUERY_LEN = 3;

    let container: HTMLDivElement | null = null;
    let renderer: Sigma | null = null;
    let graph: GraphLib | null = null;
    /**
     * Nur Paper mit geladenen Details (Hover/Klick) und per Highlight
     * eingefügte Paper. Reine Punkte leben nur in `points` + Graph-Attributen.
     */
    let paperCache = new Map<string, Paper>();
    const MAX_PAPER_CACHE = 5000;
    // Hover waits before closing
    let hoverTimeout: number | null = null;
    let hoveredPaper: Paper | null = null;
    let selectedNode: string | null = null;
    /** Track nodes added via author highlight so they can be removed on clear. */
    let authorAddedNodes: Set<string> = new Set();
    /** Track nodes added via search highlight so they can be removed on clear. */
    let searchAddedNodes: Set<string> = new Set();
    // max number of paper injected so FE doesnt crash
    const MAX_SEARCH_ADD = 100;
    /** Stored globally so added nodes can be sized consistently with existing ones. */
    let maxCit = 0;

    // ─── Highlight-Zustand (wird im nodeReducer angewendet) ───────────
    // Statt bei jedem Highlight alle Knoten per setNodeAttribute umzufärben
    // (O(n) Graph-Events), halten wir den Zustand in Sets/Maps und lassen
    // Sigma ihn beim Rendern über den nodeReducer anwenden.
    const DIM_COLOR = "rgba(255,255,255,0.08)";
    /** Search/Author-Highlight aktiv → nicht enthaltene Knoten werden gedimmt */
    let hlActive = false;
    /** Hervorgehobene Knoten → Mindestgröße */
    let hlNodes = new Map<string, number>();
    let hlColor = "";
    /** Ausgewählter Knoten (selectedPaperId) und seine Zitationen */
    let selCenter: string | null = null;
    let selCited = new Set<string>();

    const dispatch = createEventDispatcher();

    function cachePaper(p: Paper) {
        paperCache.delete(p.entry_id);
        paperCache.set(p.entry_id, p);
        while (paperCache.size > MAX_PAPER_CACHE) {
            const oldest = paperCache.keys().next().value as string;
            paperCache.delete(oldest);
        }
    }

    // ─── total citation count for node sizing ─────────────────────────
    function totalCitations(p: Paper): number {
        // Punkte aus /papers/points bringen die arXiv-Zitationszahl direkt mit,
        // volle Paper eine citations-Liste.
        const arxivCit = p.arxiv_citation_count ?? p.citations?.length ?? 0;
        const nonArxivCit = p.non_arxiv_citation_count ?? 0;

        return Math.max(
            arxivCit + nonArxivCit,
            p.non_arxiv_citation_count || 0,
        );
    }

    // Auf Deutsch zum Verständnis:
    // Wir haben einen force-directed-graphen der die Paper als Knoten darstellt.
    // Da es sein kann, dass Paper sehr nah beieinander liegen müssen wir mit möglichen Kollisionen umgehen
    // Dazu haben wir einen einfachen Algorithmus implementiert der über mehrere Iterationen hinweg alle Knoten paarweise anschaut und sie auseinander schiebt wenn sie zu nah beieinander liegen.
    // Es wird die Distanz von zwei punkten a und b berechnet (wurzel(deltaX^2 + deltaY^2)) und mit einem  Mindestabstand vergliche
    // Ist die Distanz kleiner als das minimum wird eine verschiebung berechnet, proportional zum abstand. Ein bisschen wie magnete
    // das ganze wird mehrmals wiederholt
    // hier unten weiter auf englisch mit den parameter
    // ─── spread algorithm: push apart overlapping nodes ───────────────
    // ┌─ Overlap tuning knobs ─────────────────────────────────────────
    // │  MIN_DIST      – minimum pixel gap between any two node edges.
    // │                  Increase to spread nodes further apart.
    // │  MAX_DIST      – hard cap on how far a node may drift from the
    // │                  centroid (prevents nodes flying out screen).
    // │  SPREAD_ITERS  – number of repulsion repeats; more =
    // │                  better separation but slower load.
    // │  REPEL_STRENGTH– [0-1] fraction of overlap moved per iteration.
    // └────────────────────────────────────────────────────────────────
    const n = points?.count ?? 0;
    const MIN_DIST = 2.5 * Math.sqrt(Math.max(1, n) / 1000); // minimum distance between any two nodes
    const SPREAD_ITERS = 10; //60;
    const REPEL_STRENGTH = 0.5;

    const COORD_SCALE = 60 * Math.sqrt(Math.max(1, n) / 1000); // 1000->40, 2500->~63

    const ANCHOR = 0.012 * Math.sqrt(Math.max(1, n) / 1000); // 0.12..0.25 (mehr = UMAP-treuer)
    const MAX_DRIFT = 180;

    // Spread nur bis zu dieser Knotenzahl. Darüber zeigen wir die UMAP-
    // Koordinaten unverändert – Überlappung löst sich beim Reinzoomen auf.
    const SPREAD_MAX_NODES = 10000;

    // Gleicher Algorithmus wie zuvor (paarweise Abstoßung + Anker zur
    // UMAP-Position), aber statt jedes Paar zu prüfen (O(n²)) kommen die Knoten
    // in ein Raster mit Zellgröße = größter möglicher Mindestabstand. Zwei
    // Knoten können sich nur überlappen, wenn sie in derselben oder einer
    // benachbarten Zelle liegen → pro Knoten nur 9 Zellen prüfen, ~O(n).
    // Gerechnet wird direkt auf den Typed Arrays, bevor die Knoten in den
    // Graph kommen (keine Attribut-Lese-/Schreibzugriffe).
    function applySpread(
        xs: Float64Array,
        ys: Float64Array,
        sizes: Float64Array,
    ) {
        const N = xs.length;
        if (N < 2 || N > SPREAD_MAX_NODES) return;

        const x0s = Float64Array.from(xs);
        const y0s = Float64Array.from(ys);
        let maxSize = 0;
        for (let i = 0; i < N; i++) if (sizes[i] > maxSize) maxSize = sizes[i];

        const cellSize = 2 * maxSize + MIN_DIST;
        const cellX = new Int32Array(N);
        const cellY = new Int32Array(N);
        const next = new Int32Array(N); // verkettete Liste pro Zelle
        // Halbe Nachbarschaft: jedes Zellpaar wird genau einmal besucht.
        const NEIGHBOURS: [number, number][] = [
            [1, 0],
            [-1, 1],
            [0, 1],
            [1, 1],
        ];

        const pushApart = (i: number, j: number) => {
            const dx = xs[j] - xs[i];
            const dy = ys[j] - ys[i];
            const dist = Math.sqrt(dx * dx + dy * dy) || 0.0001;
            // "nicht berühren": Abstand ~ Summe Radien + kleiner Gap
            const minDist = sizes[i] + sizes[j] + MIN_DIST;
            if (dist < minDist) {
                const push = ((minDist - dist) / 2) * REPEL_STRENGTH;
                const ux = (dx / dist) * push;
                const uy = (dy / dist) * push;
                xs[i] -= ux;
                ys[i] -= uy;
                xs[j] += ux;
                ys[j] += uy;
            }
        };

        for (let iter = 0; iter < SPREAD_ITERS; iter++) {
            // Raster neu aufbauen (Knoten haben sich bewegt)
            let minX = Infinity;
            let minY = Infinity;
            let maxX = -Infinity;
            for (let i = 0; i < N; i++) {
                if (xs[i] < minX) minX = xs[i];
                if (ys[i] < minY) minY = ys[i];
                if (xs[i] > maxX) maxX = xs[i];
            }
            const cols = Math.floor((maxX - minX) / cellSize) + 1;
            const head = new Map<number, number>();
            for (let i = 0; i < N; i++) {
                const cx = Math.floor((xs[i] - minX) / cellSize);
                const cy = Math.floor((ys[i] - minY) / cellSize);
                cellX[i] = cx;
                cellY[i] = cy;
                const key = cy * cols + cx;
                next[i] = head.get(key) ?? -1;
                head.set(key, i);
            }

            for (let i = 0; i < N; i++) {
                const cx = cellX[i];
                const cy = cellY[i];
                // gleiche Zelle: jedes Paar einmal (j > i)
                for (let j = head.get(cy * cols + cx) ?? -1; j !== -1; j = next[j]) {
                    if (j > i) pushApart(i, j);
                }
                // vier "vorwärts" liegende Nachbarzellen
                for (const [ox, oy] of NEIGHBOURS) {
                    const nx = cx + ox;
                    if (nx < 0 || nx >= cols) continue;
                    const key = (cy + oy) * cols + nx;
                    for (let j = head.get(key) ?? -1; j !== -1; j = next[j]) {
                        pushApart(i, j);
                    }
                }
            }

            // Anker zurück Richtung Originalposition + Drift clamp
            for (let i = 0; i < N; i++) {
                let x = xs[i] + (x0s[i] - xs[i]) * ANCHOR;
                let y = ys[i] + (y0s[i] - ys[i]) * ANCHOR;
                const ddx = x - x0s[i];
                const ddy = y - y0s[i];
                const d = Math.sqrt(ddx * ddx + ddy * ddy);
                if (d > MAX_DRIFT) {
                    x = x0s[i] + (ddx / d) * MAX_DRIFT;
                    y = y0s[i] + (ddy / d) * MAX_DRIFT;
                }
                xs[i] = x;
                ys[i] = y;
            }
        }
    }

    $: applyGraphHighlights(searchHighlightPapers, authorHighlight);

    // ─── reactively select node when prop changes ─────────────────────
    $: if (
        selectedPaperId &&
        selectedPaperId !== selectedNode &&
        graph &&
        renderer
    ) {
        highlightNode(selectedPaperId);
    }

    function applyGraphHighlights(searchPapers: Paper[], authorQ: string) {
        if (!graph || !renderer) return;
        if (searchPapers.length === 0 && !authorQ) {
            restoreAllNodeColors();
            return;
        }
        if (searchPapers.length > 0) {
            applySearchHighlight(searchPapers);
        } else if (authorQ) {
            handleAuthorHighlight(authorQ);
        } else {
            restoreAllNodeColors();
        }
    }

    function dropInjected(set: Set<string>) {
        if (!graph) return;
        for (const nid of set) {
            if (graph.hasNode(nid)) graph.dropNode(nid); // entfernt auch Kanten
        }
    }

    /** Remove search/author highlight state and any injected nodes. */
    function restoreAllNodeColors() {
        if (!graph || !renderer) return;
        authorHighlightToken++; // laufende Autor-Abfragen verwerfen
        dropInjected(authorAddedNodes);
        authorAddedNodes = new Set();
        dropInjected(searchAddedNodes);
        searchAddedNodes = new Set();
        hlActive = false;
        hlNodes = new Map();
        renderer.refresh();
    }

    function fullColor(): string {
        const rgb = hexToRgb(categoryColor);
        return `rgba(${rgb[0]},${rgb[1]},${rgb[2]},1.0)`;
    }

    /**
     * Highlight search results in the graph.
     * - Nodes matching a result entry_id get the category colour at full opacity.
     * - Non-matching nodes are dimmed.
     * - Papers missing from the graph are injected (up to MAX_SEARCH_ADD),
     *   sized using the same citation-count formula as regular nodes.
     */
    function applySearchHighlight(papers: Paper[]) {
        if (!graph || !renderer) return;
        authorHighlightToken++; // Suche hat Vorrang vor laufenden Autor-Abfragen

        // Remove previously injected search + author nodes before re-applying.
        dropInjected(searchAddedNodes);
        searchAddedNodes = new Set();
        dropInjected(authorAddedNodes);
        authorAddedNodes = new Set();

        const highlightColor = fullColor();
        hlColor = highlightColor;
        hlNodes = new Map();
        for (const p of papers) hlNodes.set(p.entry_id, 4);
        hlActive = true;

        // Inject missing papers (capped at MAX_SEARCH_ADD).
        let addedCount = 0;
        for (let i = 0; i < papers.length && addedCount < MAX_SEARCH_ADD; i++) {
            const p = papers[i];
            if (graph.hasNode(p.entry_id)) continue; // already shown

            const size = 3; //2 + (maxCit > 0 ? (cit / maxCit) * 6 : 2);

            // Prefer UMAP position; fall back to outward spiral around the graph.
            const hasUmap = p.tsne1 !== 0 || p.tsne2 !== 0;
            const spiralAngle = (n + i) * Math.PI * (3 - Math.sqrt(5));
            const x = hasUmap
                ? p.tsne1 * COORD_SCALE
                : 6 * Math.sqrt(n + i + 1) * Math.cos(spiralAngle);
            const y = hasUmap
                ? p.tsne2 * COORD_SCALE
                : 6 * Math.sqrt(n + i + 1) * Math.sin(spiralAngle);

            graph.addNode(p.entry_id, {
                x,
                y,
                size,
                label: p.title,
                color: highlightColor,
                zIndex: Math.ceil(size),
            });
            cachePaper(p);
            searchAddedNodes.add(p.entry_id);
            addedCount++;
        }

        renderer.refresh();
    }

    // Highlight existing graph nodes whose authors contain the search term;
    // then fetch from the API and add any matching papers not yet in the graph.
    let authorHighlightToken = 0;

    async function handleAuthorHighlight(author: string) {
        if (!graph || !renderer) return;

        // Remove any nodes we previously added for author-highlight purposes
        dropInjected(authorAddedNodes);
        authorAddedNodes = new Set();

        if (!author) {
            authorHighlightToken++; // laufende Abfragen verwerfen
            hlActive = false;
            hlNodes = new Map();
            renderer.refresh();
            return;
        }

        const q = author.toLowerCase();
        const matchColor = fullColor();
        hlColor = matchColor;

        // First pass: nur Paper mit bekannten Autoren (Details geladen) –
        // statt über alle Knoten zu iterieren.
        const matches = new Map<string, number>();
        for (const [id, p] of paperCache) {
            if (p.authors && p.authors.toLowerCase().includes(q) && graph.hasNode(id))
                matches.set(id, 3);
        }
        hlNodes = matches;
        hlActive = true;
        renderer.refresh();

        // Neuere Eingaben machen ältere, noch laufende Abfragen ungültig
        const token = ++authorHighlightToken;
        const isStale = () =>
            token !== authorHighlightToken || !graph || !renderer;

        // Backend verlangt mindestens 3 Zeichen → kürzere Eingaben nur lokal
        if (!categoryId || author.trim().length < MIN_QUERY_LEN) return;

        // Punkte aus /papers/points kennen ihre Autoren nicht (erst nach
        // Hover/Klick). Deshalb IDs ALLER Treffer der Kategorie vom Backend
        // holen und die vorhandenen Knoten damit hervorheben.
        const ids = await fetchAuthorPaperIds(apiBaseUrl, categoryId, author);
        if (isStale()) return;
        for (const id of ids) {
            if (graph!.hasNode(id) && !matches.has(id)) matches.set(id, 3);
        }
        renderer!.refresh();

        // Treffer, die (wegen Paper-Limit) nicht im Graph sind, mit ihren
        // Koordinaten nachladen und ergänzen (max. 50)
        try {
            const params = new URLSearchParams();
            params.set("search", author);
            params.set("categories", categoryId);
            params.set("take", "50"); // fetch up to 50 matching papers
            params.set("skip", "0");
            // call api endpoint (The service is PAPERS.SERVICE.TS in the backend)
            const res = await fetch(`${apiBaseUrl}/papers?${params.toString()}`);
            if (isStale()) return;
            if (res.ok) {
                const payload = await res.json();
                if (isStale()) return;
                const items: any[] = Array.isArray(payload)
                    ? payload
                    : (payload?.items ?? []);
                // Only keep papers whose author field actually matches the search term
                const matching = items.filter((raw: any) => {
                    const authors = Array.isArray(raw.authors)
                        ? raw.authors.join(", ")
                        : (raw.authors ?? "");
                    return authors.toLowerCase().includes(q);
                });
                matching.forEach((raw: any, i: number) => {
                    if (graph!.hasNode(raw.entry_id)) {
                        // Already in the graph - make sure it's highlighted
                        matches.set(raw.entry_id, 8);
                        return;
                    }
                    // Not in the graph yet - add it at a computed position
                    const hasUmap = raw.tsne1 || raw.tsne?.x;
                    const angle = (n + i) * Math.PI * (3 - Math.sqrt(5));
                    const rawTsneX = hasUmap
                        ? (raw.tsne?.x ?? raw.tsne1 ?? 0)
                        : 6 * Math.sqrt(n + i + 1) * Math.cos(angle);
                    const rawTsneY = hasUmap
                        ? (raw.tsne?.y ?? raw.tsne2 ?? 0)
                        : 6 * Math.sqrt(n + i + 1) * Math.sin(angle);

                    const x = rawTsneX * COORD_SCALE;
                    const y = rawTsneY * COORD_SCALE;

                    const authors = Array.isArray(raw.authors)
                        ? raw.authors.join(", ")
                        : (raw.authors ?? "");
                    const newPaper: Paper = {
                        id: Number(raw.id ?? 0),
                        entry_id: raw.entry_id,
                        title: raw.title ?? raw.entry_id,
                        authors,
                        abstract: raw.abstract ?? "",
                        published: raw.published ?? null,
                        categories: raw.categories ?? null,
                        url: raw.url ?? null,
                        citations: Array.isArray(raw.citations)
                            ? raw.citations
                            : [],
                        references: [],
                        non_arxiv_citation_count: Number(
                            raw.non_arxiv_citation_count ?? 0,
                        ),
                        non_arxiv_reference_count: Number(
                            raw.non_arxiv_reference_count ?? 0,
                        ),
                        tsne1: rawTsneX,
                        tsne2: rawTsneY,
                        cluster: raw.categories ?? "U",
                    };
                    const size =
                        2 +
                        (maxCit > 0
                            ? (totalCitations(newPaper) / maxCit) * 6
                            : 2);
                    graph!.addNode(raw.entry_id, {
                        x,
                        y,
                        // Size proportional to citations, same formula as regular nodes.
                        size,
                        label: raw.title ?? raw.entry_id,
                        color: matchColor,
                        zIndex: 10,
                    });
                    cachePaper(newPaper);
                    authorAddedNodes.add(raw.entry_id);
                    matches.set(raw.entry_id, size);
                });
            }
        } catch {
            /* ignore network errors silently */
        }
        if (!isStale()) renderer!.refresh();
    }

    async function highlightNode(nodeId: string) {
        if (!graph || !renderer || !graph.hasNode(nodeId)) return;
        selectedNode = nodeId;

        // 1. Alte Kanten löschen, Fokus setzen (Dimmen übernimmt der Reducer)
        graph.clearEdges();
        selCenter = nodeId;
        selCited = new Set();
        renderer.refresh();

        // 2. Zitationen nachladen, falls nötig
        const paper = await ensureDetails(nodeId);
        if (!graph || !renderer || selCenter !== nodeId) return;

        // 3. Kanten zu Zitationen zeichnen (falls diese im Graphen existieren)
        if (paper) {
            for (const cid of paper.citations) {
                if (!graph.hasNode(cid) || selCited.has(cid)) continue;
                selCited.add(cid);
                graph.addEdge(nodeId, cid, {
                    color: "rgba(255,255,255,0.3)",
                    size: 1.5, // Etwas dicker für bessere Sichtbarkeit
                });
            }
        }
        renderer.refresh();
    }

    // ─── Details bei Bedarf nachladen ───────────────────────────────────
    // Punkte aus /papers/points enthalten nur ID, Koordinaten und
    // Zitationszahlen. Titel, Autoren, Abstract kommen erst bei Hover/Klick.
    // Laufende Requests werden pro Paper geteilt, damit Hover + Klick nicht
    // doppelt laden.
    const detailRequests = new Map<string, Promise<Paper | null>>();
    const detailsAbort = new AbortController();

    /** Paper aus dem Cache oder (ohne Details) aus der Punktwolke. */
    function getPaper(entryId: string): Paper | null {
        const cached = paperCache.get(entryId);
        if (cached) return cached;
        if (!graph || !graph.hasNode(entryId)) return null;
        const i = graph.getNodeAttribute(entryId, "i");
        return typeof i === "number" ? pointToPaper(points, i) : null;
    }

    function ensureDetails(entryId: string): Promise<Paper | null> {
        const cached = paperCache.get(entryId);
        if (cached && cached.detailsLoaded !== false)
            return Promise.resolve(cached);
        let req = detailRequests.get(entryId);
        if (!req) {
            req = fetchPaperDetails(apiBaseUrl, entryId, detailsAbort.signal)
                .then((details) => {
                    const point = getPaper(entryId);
                    if (!details || !point) return null;
                    const full = mergeDetails(point, details);
                    cachePaper(full);
                    return full;
                })
                .catch(() => null)
                .finally(() => detailRequests.delete(entryId));
            detailRequests.set(entryId, req);
        }
        return req;
    }

    onMount(() => {
        if (!container) return;
        graph = new GraphLib();

        const N = n;
        const { ids, x: px, y: py, cc, nc } = points;

        // Compute max total citations for node sizing (stored at module level
        // so that nodes added later for highlights use the same scale).
        maxCit = 0;
        for (let i = 0; i < N; i++) {
            const c = cc[i] + nc[i];
            if (c > maxCit) maxCit = c;
        }

        // Positionen + Größen auf Typed Arrays vorberechnen, Spread darauf
        // anwenden und erst danach jeden Knoten genau einmal einfügen.
        const xs = new Float64Array(N);
        const ys = new Float64Array(N);
        const sizes = new Float64Array(N);
        let missingUmap = 0;
        for (let i = 0; i < N; i++) {
            // Use UMAP coordinates when available, fallback to spiral
            const hasUmap = px[i] !== 0 || py[i] !== 0;
            if (!hasUmap) missingUmap++;
            const angle = i * Math.PI * (3 - Math.sqrt(5));
            xs[i] = hasUmap
                ? px[i] * COORD_SCALE
                : 6 * Math.sqrt(i + 1) * Math.cos(angle);
            ys[i] = hasUmap
                ? py[i] * COORD_SCALE
                : 6 * Math.sqrt(i + 1) * Math.sin(angle);
            const cit = cc[i] + nc[i];
            // normal sind nodes 2 einheiten groß. wachsen bis zu faktor 6 bei sehr vielen zitationen
            sizes[i] = 2 + (maxCit > 0 ? (cit / maxCit) * 4 : 0);
        }

        if (missingUmap > 0) {
            console.warn(
                `${missingUmap} papers without UMAP coordinates, using fallback layout.`,
            );
        }

        // Apply spread to prevent overlapping nodes
        applySpread(xs, ys, sizes);

        // Farbe hängt nur von Kategorie + (gerundeter) Zitationszahl ab →
        // Strings werden geteilt statt pro Knoten neu erzeugt.
        const rgb = hexToRgb(categoryColor);
        const colorByAlpha = new Map<number, string>();
        for (let i = 0; i < N; i++) {
            const cit = cc[i] + nc[i];
            const alphaKey = Math.round(
                (0.4 + (maxCit > 0 ? (cit / maxCit) * 0.6 : 0.3)) * 100,
            );
            let color = colorByAlpha.get(alphaKey);
            if (!color) {
                color = `rgba(${rgb[0]},${rgb[1]},${rgb[2]},${alphaKey / 100})`;
                colorByAlpha.set(alphaKey, color);
            }
            if (graph.hasNode(ids[i])) continue; // doppelte IDs ignorieren
            graph.addNode(ids[i], {
                x: xs[i],
                y: ys[i],
                size: sizes[i],
                color,
                zIndex: Math.ceil(sizes[i]),
                i, // Index in `points` für Details/Tooltip
            });
        }

        renderer = new Sigma(graph, container, {
            renderEdgeLabels: false,
            defaultNodeType: "circle",
            defaultEdgeType: "line",
            minCameraRatio: n >= 1000 ? 0.001 : 0.08,
            maxCameraRatio: 5,
            renderLabels: false,
            labelRenderedSizeThreshold: 8,
            zIndex: true,
            nodeReducer: (node, data) => {
                if (hlActive) {
                    const minSize = hlNodes.get(node);
                    if (minSize !== undefined)
                        return {
                            ...data,
                            color: hlColor,
                            size: Math.max(data.size, minSize),
                            zIndex: (data.zIndex ?? 0) + 10,
                        };
                    return { ...data, color: DIM_COLOR };
                }
                if (selCenter) {
                    if (node === selCenter)
                        return { ...data, color: "#00ff88", zIndex: 20 };
                    if (selCited.has(node))
                        return { ...data, color: "#ffcc00", zIndex: 15 };
                    return { ...data, color: DIM_COLOR };
                }
                return data;
            },
        });

        renderer.on("enterNode", ({ node }) => {
            if (hoverTimeout) clearTimeout(hoverTimeout);
            hoverTimeout = setTimeout(() => {
                hoveredPaper = getPaper(node);
                if (hoveredPaper && hoveredPaper.detailsLoaded === false) {
                    ensureDetails(node).then((full) => {
                        // nur übernehmen, wenn der Nutzer noch auf dem Knoten ist
                        if (full && hoveredPaper?.entry_id === node)
                            hoveredPaper = full;
                    });
                }
            }, 150) as unknown as number;
        });
        renderer.on("leaveNode", () => {
            if (hoverTimeout) clearTimeout(hoverTimeout);
            hoveredPaper = null;
        });

        renderer.on("clickNode", async ({ node }) => {
            const paper = getPaper(node);
            if (!paper) return;
            // Detailansicht braucht Titel, Autoren usw. → vorher nachladen
            const full =
                paper.detailsLoaded === false
                    ? ((await ensureDetails(node)) ?? paper)
                    : paper;
            if (!renderer) return; // Komponente inzwischen zerstört
            hoveredPaper = null;
            dispatch("paperSelected", full);
        });

        renderer.on("clickStage", () => {
            selectedNode = null;
            hoveredPaper = null;
            selCenter = null;
            selCited = new Set();
            // wie bisher: Klick ins Leere setzt auch die Highlight-Färbung zurück
            hlActive = false;
            graph?.clearEdges();
            renderer!.refresh();
            dispatch("nodeDeselected");
        });

        // Highlights, die vor dem Mount gesetzt wurden, jetzt anwenden
        applyGraphHighlights(searchHighlightPapers, authorHighlight);

        return () => {
            if (hoverTimeout) clearTimeout(hoverTimeout);
            authorHighlightToken++;
            detailsAbort.abort();
            detailRequests.clear();
            if (renderer) {
                renderer.kill();
                renderer = null;
            }
            graph = null;
            paperCache.clear();
        };
    });

    function hexToRgb(hex: string): [number, number, number] {
        const m = hex.match(/^#?([\da-f]{2})([\da-f]{2})([\da-f]{2})$/i);
        if (!m) return [74, 158, 255];
        return [parseInt(m[1], 16), parseInt(m[2], 16), parseInt(m[3], 16)];
    }
</script>

<div class="graph-wrapper">
    <div class="sigma-container" bind:this={container}></div>

    {#if hoveredPaper}
        <div class="tooltip">
            {#if hoveredPaper.detailsLoaded === false}
                <p class="meta">Lade Details…</p>
            {:else}
            <h4>{hoveredPaper.title}</h4>
            <p class="meta">{hoveredPaper.authors}</p>
            <p class="meta">
                {hoveredPaper.published
                    ? new Date(hoveredPaper.published).toLocaleDateString()
                    : ""}
            </p>
            <p class="abstract">
                {hoveredPaper.abstract.slice(0, 200)}{hoveredPaper.abstract
                    .length > 200
                    ? "…"
                    : ""}
            </p>
            <p class="stats">
                Citations: {hoveredPaper.citations.length} | References: {hoveredPaper
                    .references.length} | Non-arXiv: {(hoveredPaper.non_arxiv_citation_count ??
                    0) + (hoveredPaper.non_arxiv_reference_count ?? 0)}
            </p>
            {/if}
        </div>
    {/if}
</div>

<style>
    .graph-wrapper {
        width: 100%;
        height: 100%;
        position: relative;
        background: var(--bg-primary, #0f1020);
    }
    .sigma-container {
        width: 100%;
        height: 100%;
    }
    .tooltip {
        position: absolute;
        top: 12px;
        right: 12px;
        max-width: 340px;
        background: var(--glass-bg, rgba(20, 22, 50, 0.55));
        border: 1px solid var(--border-subtle, rgba(147, 51, 234, 0.18));
        border-radius: var(--radius-md, 12px);
        padding: 14px 16px;
        color: var(--text-primary, #f0f0f8);
        font-size: 13px;
        z-index: 10;
        pointer-events: none;
        backdrop-filter: blur(var(--glass-blur, 16px));
        box-shadow: var(--shadow-glow-sm);
    }
    .tooltip h4 {
        margin: 0 0 6px;
        font-size: 14px;
        color: var(--text-primary, #f0f0f8);
        line-height: 1.3;
        font-weight: 600;
    }
    .tooltip .meta {
        margin: 2px 0;
        color: var(--text-muted, #6b6b8d);
        font-size: 12px;
    }
    .tooltip .abstract {
        margin: 8px 0 4px;
        line-height: 1.4;
        color: var(--text-secondary, #a8a8c8);
    }
    .tooltip .stats {
        margin: 6px 0 0;
        color: var(--accent-cyan, #22d3ee);
        font-size: 11px;
        opacity: 0.8;
    }
</style>
