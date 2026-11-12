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
    import type { Paper } from "$lib/types/paper";
    import {
        fetchAuthorPaperIds,
        fetchPaperDetails,
        mergeDetails,
    } from "../utils/paperApi";

    export let papers: Paper[] = [];
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

    let container: HTMLDivElement | null = null;
    let renderer: Sigma | null = null;
    let graph: GraphLib | null = null;
    let paperCache = new Map<string, Paper>();
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

    const dispatch = createEventDispatcher();

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
    const n = papers.length;
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
    // Gerechnet wird auf Typed Arrays; ins Graph-Objekt wird nur am Ende
    // geschrieben (graphology-Attributzugriffe sind teure Map-Lookups).
    function applySpread(g: GraphLib) {
        const nodes = g.nodes();
        const N = nodes.length;
        if (N < 2 || N > SPREAD_MAX_NODES) return;

        const xs = new Float64Array(N);
        const ys = new Float64Array(N);
        const x0s = new Float64Array(N);
        const y0s = new Float64Array(N);
        const sizes = new Float64Array(N);
        let maxSize = 0;
        for (let i = 0; i < N; i++) {
            const a = g.getNodeAttributes(nodes[i]);
            xs[i] = a.x;
            ys[i] = a.y;
            x0s[i] = a.x0 ?? a.x;
            y0s[i] = a.y0 ?? a.y;
            sizes[i] = a.size || 2;
            if (sizes[i] > maxSize) maxSize = sizes[i];
        }

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

        for (let i = 0; i < N; i++) {
            g.setNodeAttribute(nodes[i], "x", xs[i]);
            g.setNodeAttribute(nodes[i], "y", ys[i]);
        }
    }

    $: applyGraphHighlights(searchHighlightPapers, authorHighlight);

    $: if (selectedPaperId && graph && renderer) {
        // Wir prüfen, ob sich die ID wirklich geändert hat,
        // um unnötige Server-Anfragen zu vermeiden
        if (selectedPaperId !== selectedNode) {
            highlightNode(selectedPaperId);
        }
    }

    function applyGraphHighlights(searchPapers: Paper[], authorQ: string) {
        if (!graph || !renderer) return;
        if (searchPapers.length === 0 && !authorQ) {
            restoreAllNodeColors();
            // Falls trotzdem ein Paper ausgewählt ist, zeichnen wir dessen Highlights neu
            if (selectedPaperId) highlightNode(selectedPaperId);
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

    /** Restore all nodes to their original colours and remove any injected nodes. */
    function restoreAllNodeColors() {
        if (!graph || !renderer) return;
        // Remove author-added nodes
        for (const nid of authorAddedNodes) {
            if (graph.hasNode(nid)) {
                graph.edges(nid).forEach((e) => graph!.dropEdge(e));
                graph.dropNode(nid);
            }
        }
        authorAddedNodes = new Set();
        // Remove search-added nodes
        for (const nid of searchAddedNodes) {
            if (graph.hasNode(nid)) {
                graph.edges(nid).forEach((e) => graph!.dropEdge(e));
                graph.dropNode(nid);
            }
        }
        searchAddedNodes = new Set();
        // Restore original colours
        graph.forEachNode((n) => {
            graph!.setNodeAttribute(
                n,
                "color",
                graph!.getNodeAttribute(n, "originalColor"),
            );
        });
        renderer.refresh();
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

        // Remove previously injected search nodes before re-applying.
        for (const nid of searchAddedNodes) {
            if (graph.hasNode(nid)) {
                graph.edges(nid).forEach((e) => graph!.dropEdge(e));
                graph.dropNode(nid);
            }
        }
        searchAddedNodes = new Set();
        // Also remove author-added nodes since search takes priority.
        for (const nid of authorAddedNodes) {
            if (graph.hasNode(nid)) {
                graph.edges(nid).forEach((e) => graph!.dropEdge(e));
                graph.dropNode(nid);
            }
        }
        authorAddedNodes = new Set();

        const entryIds = new Set(papers.map((p) => p.entry_id));
        const rgb = hexToRgb(categoryColor);
        // Full-opacity category colour for highlighted (found) nodes.
        const highlightColor = `rgba(${rgb[0]},${rgb[1]},${rgb[2]},1.0)`;

        // First pass: colour existing nodes.
        graph.forEachNode((n) => {
            if (entryIds.has(n)) {
                graph!.setNodeAttribute(n, "color", highlightColor);
                graph!.setNodeAttribute(n, "size", 4);
            } else {
                graph!.setNodeAttribute(n, "color", "rgba(255,255,255,0.08)");
            }
        });

        // Second pass: inject missing papers (capped at MAX_SEARCH_ADD).
        let addedCount = 0;
        for (let i = 0; i < papers.length && addedCount < MAX_SEARCH_ADD; i++) {
            const p = papers[i];
            if (graph.hasNode(p.entry_id)) continue; // already shown

            // Size follows the same formula as regular nodes so proportions match.
            const cit = totalCitations(p);
            const size = 3; //2 + (maxCit > 0 ? (cit / maxCit) * 6 : 2);

            // Prefer UMAP position; fall back to outward spiral around the graph.
            const hasUmap = p.tsne1 !== 0 || p.tsne2 !== 0;
            const spiralAngle =
                (papers.length + i) * Math.PI * (3 - Math.sqrt(5));
            const x = hasUmap
                ? p.tsne1 * COORD_SCALE
                : 6 * Math.sqrt(papers.length + i + 1) * Math.cos(spiralAngle);
            const y = hasUmap
                ? p.tsne2 * COORD_SCALE
                : 6 * Math.sqrt(papers.length + i + 1) * Math.sin(spiralAngle);

            graph.addNode(p.entry_id, {
                x,
                y,
                x0: x,
                y0: y,
                size,
                label: p.title,
                color: highlightColor,
                originalColor: highlightColor,
                zIndex: Math.ceil(size),
            });
            paperCache.set(p.entry_id, p);
            searchAddedNodes.add(p.entry_id);
            addedCount++;
        }

        renderer.refresh();
    }

    // Highlight existing graph nodes whose authors contain the search term;
    // then ALWAYS fetch from the API and add any matching papers not yet in the graph.
    let authorHighlightToken = 0;

    async function handleAuthorHighlight(author: string) {
        if (!graph || !renderer) return;

        // Remove any nodes we previously added for author-highlight purposes
        for (const nid of authorAddedNodes) {
            if (graph.hasNode(nid)) {
                graph.edges(nid).forEach((e) => graph!.dropEdge(e));
                graph.dropNode(nid);
            }
        }
        authorAddedNodes = new Set();

        if (!author) {
            authorHighlightToken++; // laufende Abfragen verwerfen
            // Empty author string = clear all highlights, restore original colors
            graph.forEachNode((n) => {
                graph!.setNodeAttribute(
                    n,
                    "color",
                    graph!.getNodeAttribute(n, "originalColor"),
                );
            });
            renderer.refresh();
            return;
        }

        const q = author.toLowerCase();
        // Resolve category colour to full-opacity rgba for highlighted nodes.
        const rgb = hexToRgb(categoryColor);
        const matchColor = `rgba(${rgb[0]},${rgb[1]},${rgb[2]},1.0)`;

        // First pass: highlight existing nodes or dim them
        graph.forEachNode((n) => {
            const p = paperCache.get(n);
            if (p && p.authors.toLowerCase().includes(q)) {
                // Category colour (full opacity) for matching nodes
                graph!.setNodeAttribute(n, "color", matchColor);
                graph!.setNodeAttribute(
                    n,
                    "size",
                    Math.max(graph!.getNodeAttribute(n, "size"), 3),
                );
            } else {
                // Dim non-matching nodes
                graph!.setNodeAttribute(n, "color", "rgba(255,255,255,0.08)");
            }
        });

        // Neuere Eingaben machen ältere, noch laufende Abfragen ungültig
        const token = ++authorHighlightToken;
        const isStale = () =>
            token !== authorHighlightToken || !graph || !renderer;

        // Punkte aus /papers/points kennen ihre Autoren nicht (erst nach
        // Hover/Klick). Deshalb IDs ALLER Treffer der Kategorie vom Backend
        // holen und die vorhandenen Knoten damit hervorheben.
        if (categoryId) {
            const ids = await fetchAuthorPaperIds(
                apiBaseUrl,
                categoryId,
                author,
            );
            if (isStale()) return;
            for (const id of ids) {
                if (!graph!.hasNode(id)) continue;
                graph!.setNodeAttribute(id, "color", matchColor);
                graph!.setNodeAttribute(
                    id,
                    "size",
                    Math.max(graph!.getNodeAttribute(id, "size"), 3),
                );
            }
        }

        // Treffer, die (wegen Paper-Limit) nicht im Graph sind, mit ihren
        // Koordinaten nachladen und ergänzen (max. 50)
        if (categoryId) {
            try {
                const params = new URLSearchParams();
                params.set("search", author);
                params.set("categories", categoryId);
                params.set("take", "50"); // fetch up to 50 matching papers
                params.set("skip", "0");
                // call api endpoint (The service is PAPERS.SERVICE.TS in the backend)
                const res = await fetch(
                    `${apiBaseUrl}/papers?${params.toString()}`,
                );
                if (isStale()) return;
                if (res.ok) {
                    const payload = await res.json();
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
                            graph!.setNodeAttribute(
                                raw.entry_id,
                                "color",
                                matchColor,
                            );
                            graph!.setNodeAttribute(raw.entry_id, "size", 8);
                        } else {
                            // Not in the graph yet - add it at a computed position
                            const hasUmap = raw.tsne1 || raw.tsne?.x;
                            const rawTsneX = hasUmap
                                ? (raw.tsne?.x ?? raw.tsne1 ?? 0)
                                : 6 *
                                  Math.sqrt(papers.length + i + 1) *
                                  Math.cos(
                                      (papers.length + i) *
                                          Math.PI *
                                          (3 - Math.sqrt(5)),
                                  );
                            const rawTsneY = hasUmap
                                ? (raw.tsne?.y ?? raw.tsne2 ?? 0)
                                : 6 *
                                  Math.sqrt(papers.length + i + 1) *
                                  Math.sin(
                                      (papers.length + i) *
                                          Math.PI *
                                          (3 - Math.sqrt(5)),
                                  );

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
                            graph!.addNode(raw.entry_id, {
                                x,
                                y,
                                x0: x,
                                y0: y,
                                // Size proportional to citations, same formula as regular nodes.
                                size:
                                    2 +
                                    (maxCit > 0
                                        ? (totalCitations(newPaper) / maxCit) *
                                          6
                                        : 2),
                                label: raw.title ?? raw.entry_id,
                                color: matchColor,
                                originalColor: matchColor,
                                zIndex: 10,
                            });
                            paperCache.set(raw.entry_id, newPaper);
                            authorAddedNodes.add(raw.entry_id);
                        }
                    });
                }
            } catch {
                /* ignore network errors silently */
            }
        }
        renderer.refresh();
    }

    // ─── reactively select node when prop changes ─────────────────────
    $: if (
        selectedPaperId &&
        selectedPaperId !== selectedNode &&
        graph &&
        renderer
    ) {
        highlightNode(selectedPaperId);
    }

    async function highlightNode(nodeId: string) {
        if (!graph || !renderer || !graph.hasNode(nodeId)) return;

        // 1. Alle anderen Knoten dimmen und alte Kanten löschen
        graph.forEachNode((n) => {
            graph!.setNodeAttribute(n, "color", "rgba(255,255,255,0.08)");
        });
        graph.edges().forEach((e) => graph!.dropEdge(e));

        // 2. Den Fokus-Knoten hervorheben
        graph.setNodeAttribute(nodeId, "color", "#00ff88");

        let paper = paperCache.get(nodeId);

        // --- NEU: Zitationen nachladen, falls die Liste leer ist ---
        if (
            paper &&
            paper.citations.length === 0 &&
            (paper.non_arxiv_citation_count ?? 0) === 0
        ) {
            try {
                const res = await fetch(
                    `${apiBaseUrl}/papers/${encodeURIComponent(nodeId)}`,
                );
                if (res.ok) {
                    const fullPaper = await res.json();
                    paper.citations = fullPaper.citations || [];
                    // Cache aktualisieren
                    paperCache.set(nodeId, paper);
                }
            } catch (e) {
                console.error("Graph: Failed to fetch citation details", e);
            }
        }

        // 3. Kanten zu Zitationen zeichnen (falls diese im Graphen existieren)
        if (paper) {
            paper.citations.forEach((cid) => {
                if (graph!.hasNode(cid)) {
                    graph!.setNodeAttribute(cid, "color", "#ffcc00");
                    graph!.addEdge(nodeId, cid, {
                        color: "rgba(255,255,255,0.3)",
                        size: 1.5, // Etwas dicker für bessere Sichtbarkeit
                    });
                }
            });
        }

        selectedNode = nodeId;
        renderer.refresh();
    }

    // ─── Details bei Bedarf nachladen ───────────────────────────────────
    // Punkte aus /papers/points enthalten nur ID, Koordinaten und
    // Zitationszahlen. Titel, Autoren, Abstract kommen erst bei Hover/Klick.
    // Laufende Requests werden pro Paper geteilt, damit Hover + Klick nicht
    // doppelt laden.
    const detailRequests = new Map<string, Promise<Paper | null>>();
    const detailsAbort = new AbortController();

    function ensureDetails(entryId: string): Promise<Paper | null> {
        const cached = paperCache.get(entryId);
        if (cached && cached.detailsLoaded !== false)
            return Promise.resolve(cached);
        let req = detailRequests.get(entryId);
        if (!req) {
            req = fetchPaperDetails(apiBaseUrl, entryId, detailsAbort.signal)
                .then((details) => {
                    const point = paperCache.get(entryId);
                    if (!details || !point) return null;
                    const full = mergeDetails(point, details);
                    paperCache.set(entryId, full);
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

        // Compute max total citations for node sizing (stored at module level
        // so that nodes added later for highlights use the same scale).
        maxCit = 0;
        papers.forEach((p) => {
            const c = totalCitations(p);
            if (c > maxCit) maxCit = c;
        });

        // Farbe hängt nur von der Kategorie ab → einmal statt pro Knoten
        const rgb = hexToRgb(categoryColor);
        let missingUmap = 0;
        papers.forEach((paper, i) => {
            // Use UMAP coordinates (tsne1/tsne2) when available, fallback to spiral
            const hasUmap = paper.tsne1 !== 0 || paper.tsne2 !== 0;
            if (!hasUmap) missingUmap++;
            const x = hasUmap
                ? paper.tsne1 * COORD_SCALE
                : 6 *
                  Math.sqrt(i + 1) *
                  Math.cos(i * Math.PI * (3 - Math.sqrt(5)));
            const y = hasUmap
                ? paper.tsne2 * COORD_SCALE
                : 6 *
                  Math.sqrt(i + 1) *
                  Math.sin(i * Math.PI * (3 - Math.sqrt(5)));

            const cit = totalCitations(paper);
            // bei cit == maxCit => 2 + 8 = 10
            // normal sind nodes 2 einheiten groß. wachsen bis zu faktor 6 bei sehr vielen zitationen
            const size = 2 + (maxCit > 0 ? (cit / maxCit) * 4 : 0);
            const alpha = 0.4 + (maxCit > 0 ? (cit / maxCit) * 0.6 : 0.3);
            const color = `rgba(${rgb[0]},${rgb[1]},${rgb[2]},${alpha})`;

            graph!.addNode(paper.entry_id, {
                x,
                y,
                x0: x,
                y0: y,
                size,
                label: paper.title,
                color,
                originalColor: color,
                zIndex: Math.ceil(size),
            });
            paperCache.set(paper.entry_id, paper);
        });

        if (missingUmap > 0) {
            console.warn(
                `${missingUmap} papers without UMAP coordinates, using fallback layout.`,
            );
        }

        // Apply spread to prevent overlapping nodes
        applySpread(graph);

        renderer = new Sigma(graph, container, {
            renderEdgeLabels: false,
            defaultNodeType: "circle",
            defaultEdgeType: "line",
            minCameraRatio: n >= 1000 ? 0.001 : 0.08,
            maxCameraRatio: 5,
            renderLabels: false,
            labelRenderedSizeThreshold: 8,
            zIndex: true,
        });

        renderer.on("enterNode", ({ node }) => {
            if (hoverTimeout) clearTimeout(hoverTimeout);
            hoverTimeout = setTimeout(() => {
                hoveredPaper = paperCache.get(node) || null;
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
            const paper = paperCache.get(node);
            if (!paper) return;
            // Detailansicht braucht Titel, Autoren usw. → vorher nachladen
            const full =
                paper.detailsLoaded === false
                    ? ((await ensureDetails(node)) ?? paper)
                    : paper;
            if (!renderer) return; // Komponente inzwischen zerstört
            dispatch("paperSelected", full);
        });

        renderer.on("clickStage", () => {
            selectedNode = null;
            hoveredPaper = null;
            if (graph) {
                graph.forEachNode((n) => {
                    graph!.setNodeAttribute(
                        n,
                        "color",
                        graph!.getNodeAttribute(n, "originalColor"),
                    );
                });
                graph.edges().forEach((e) => graph!.dropEdge(e));
            }
            renderer!.refresh();
            dispatch("nodeDeselected");
        });

        return () => {
            if (hoverTimeout) clearTimeout(hoverTimeout);
            detailsAbort.abort();
            detailRequests.clear();
            if (renderer) {
                renderer.kill();
                renderer = null;
            }
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
