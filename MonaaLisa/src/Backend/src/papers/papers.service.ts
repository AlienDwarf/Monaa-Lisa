import { Injectable } from '@nestjs/common';
import { PrismaService } from '../prisma/prisma.service';
import { Prisma } from '@prisma/client';
import { CreatePaperDto } from './dto/create-paper.dto';
import { UpdatePaperDto } from './dto/update-paper.dto';
import {
  COUNT_CAP,
  MAX_PAGE_SIZE,
  MAX_SKIP,
  NeighbourFilterDto,
  QueryPaperDto,
} from './dto/query-paper.dto';
import { CachedPayload, PointsCache } from './points-cache';

/** Titel von Platzhalter-Zeilen, die nie ausgeliefert werden. */
export const STUB_TITLE = '[STUB] Pending Fetch';

/** Max. Zitationen/Referenzen, die an ein einzelnes Paper angehängt werden. */
const MAX_INLINE_LINKS = 10_000;

/**
 * Spaltenorientierte Punktwolke einer Kategorie (GET /papers/points).
 * Index i in allen Arrays gehört zum selben Paper. Spalten statt Objekte,
 * weil das JSON dadurch etwa halb so groß ist und schneller geparst wird.
 */
export interface CategoryPoints {
  category: string;
  count: number;
  ids: string[];
  x: number[];
  y: number[];
  /** arXiv-interne Zitationen (Zeilen in paper_citation) */
  cc: number[];
  /** Zitationen von außerhalb arXiv */
  nc: number[];
}

type PointRow = {
  id: string;
  x: number;
  y: number;
  cc: number;
  nc: number | null;
};

function envInt(name: string, fallback: number): number {
  const v = Number(process.env[name]);
  return Number.isFinite(v) && v > 0 ? v : fallback;
}

/** Auf 4 Nachkommastellen runden – hält das JSON klein. */
function round4(v: number): number {
  return Math.round(v * 1e4) / 1e4;
}

/**
 * PapersService Nico
 * -------------------------------
 * Kümmert sich um alles rund um "Paper":
 * - Anlegen & Upserten (für idempotente Ingestion)
 * - Suchen & Paginieren
 * - Lesen, Updaten, Löschen
 *
 * Hinweis: Wir normalisieren Datumsfelder mit `new Date(...)`
 * und setzen serverseitig ein `added`-Timestamp.
 */
@Injectable()
export class PapersService {
  constructor(private prisma: PrismaService) {}

  private readonly pointsCache = new PointsCache(
    envInt('POINTS_CACHE_MB', 256) * 1024 * 1024,
    envInt('POINTS_CACHE_TTL_S', 3600) * 1000,
  );

  /**
   * Legt ein neues Paper an.
   * - `published` ist optional → wenn nicht da, speichern wir `null`
   * - `added` ist der Zeitpunkt, zu dem wir es in unser System aufnehmen
   */
  async create(dto: CreatePaperDto) {
    this.pointsCache.clear(); // Punktwolken können sich geändert haben
    return this.prisma.paper.create({
      data: {
        entry_id: dto.entry_id,
        title: dto.title,
        authors: authorsToString(dto.authors) ?? null,
        abstract: dto.abstract ?? null,
        categories: dto.categories ?? null,
        ...deriveCategoryColumns(dto.categories),
        published: dto.published ? new Date(dto.published) : null,
        updated: dto.updated ? new Date(dto.updated) : null,
        doi: dto.doi ?? null,
        journal_ref: dto.journal_ref ?? null,
        license: dto.license ?? null,
        url: dto.url ?? null,
        s2_id: dto.s2_id ?? null,
        non_arxiv_citation_count: dto.non_arxiv_citation_count ?? null,
        non_arxiv_reference_count: dto.non_arxiv_reference_count ?? null,
      },
    });
  }

  /**
   * Filtert eine Liste von bekannten entry_ids nach den Kriterien aus der UI.
   * Das verhindert, dass wir zehntausende Paper ins Frontend laden müssen.
   */
  async findFilteredNeighbours(
    entryIds: string[],
    filter: NeighbourFilterDto = {},
  ) {
    if (!entryIds || entryIds.length === 0) return [];

    // Kandidaten sind über den Unique-Index auf entry_id eingegrenzt; die
    // restlichen Filter laufen nur noch über diese (max. 20k) Zeilen.
    const where: Prisma.PaperWhereInput = {
      entry_id: { in: [...new Set(entryIds)] },
      title: { not: STUB_TITLE },
    };

    if (filter.titleQuery && filter.titleQuery.trim() !== '') {
      where.AND = [
        { title: { contains: filter.titleQuery.trim(), mode: 'insensitive' } },
      ];
    }

    // 1. Datumsfilter
    if (filter.dateFrom || filter.dateTo) {
      const publishedFilter: Prisma.DateTimeNullableFilter = {};
      if (filter.dateFrom) publishedFilter.gte = new Date(filter.dateFrom);
      if (filter.dateTo) publishedFilter.lte = new Date(filter.dateTo);
      where.published = publishedFilter;
    }

    // 2. Autorenfilter (Case-Insensitive Suche in Prisma)
    if (filter.authorQuery && filter.authorQuery.trim() !== '') {
      where.authors = {
        contains: filter.authorQuery.trim(),
        mode: 'insensitive',
      };
    }

    // 3. Abstractfilter (Case-Insensitive Suche in Prisma)
    if (filter.abstractQuery && filter.abstractQuery.trim() !== '') {
      where.abstract = {
        contains: filter.abstractQuery.trim(),
        mode: 'insensitive',
      };
    }

    // 4. Minimum Citations
    if (filter.minCitations !== undefined && filter.minCitations >= 0) {
      where.non_arxiv_citation_count = {
        gte: filter.minCitations,
      };
    }

    // Wir holen nur die Felder, die die Sidebar wirklich braucht,
    // um Bandbreite und RAM zu sparen.
    const papers = await this.prisma.paper.findMany({
      where,
      select: {
        entry_id: true,
        title: true,
        authors: true,
        abstract: true,
        published: true,
        categories: true,
        non_arxiv_citation_count: true,
        non_arxiv_reference_count: true,
      },
      // Ein Limit als Sicherheitsgurt, falls ein Filter zu "weich" ist
      take: 1000,
    });

    return papers;
  }

  // Idempotente Ingestion: Upsert über entry_id
  /**
   * Upsert (Create ODER Update) anhand `entry_id`.
   * Warum? Ingestion kann dasselbe Item mehrfach sehen – hiermit vermeiden wir Duplikate.
   */
  async upsertByEntryId(dto: CreatePaperDto) {
    this.pointsCache.clear(); // Punktwolken können sich geändert haben
    return this.prisma.paper.upsert({
      where: { entry_id: dto.entry_id },
      update: {
        title: dto.title,
        authors: authorsToString(dto.authors) ?? null,
        abstract: dto.abstract ?? null,
        categories: dto.categories ?? null,
        ...deriveCategoryColumns(dto.categories),
        published: dto.published ? new Date(dto.published) : null,
        updated: dto.updated ? new Date(dto.updated) : null,
        doi: dto.doi ?? null,
        journal_ref: dto.journal_ref ?? null,
        license: dto.license ?? null,
        url: dto.url ?? null,
        s2_id: dto.s2_id ?? null,
        non_arxiv_citation_count: dto.non_arxiv_citation_count ?? null,
        non_arxiv_reference_count: dto.non_arxiv_reference_count ?? null,
      },
      create: {
        entry_id: dto.entry_id,
        title: dto.title,
        authors: authorsToString(dto.authors) ?? null,
        abstract: dto.abstract ?? null,
        categories: dto.categories ?? null,
        ...deriveCategoryColumns(dto.categories),
        published: dto.published ? new Date(dto.published) : null,
        updated: dto.updated ? new Date(dto.updated) : null,
        doi: dto.doi ?? null,
        journal_ref: dto.journal_ref ?? null,
        license: dto.license ?? null,
        url: dto.url ?? null,
        s2_id: dto.s2_id ?? null,
        non_arxiv_citation_count: dto.non_arxiv_citation_count ?? null,
        non_arxiv_reference_count: dto.non_arxiv_reference_count ?? null,
      },
    });
  }

  /**
   * Sucht Papers mit Volltext-Like über `title`, `authors`, `summary`,
   * plus optionaler Kategorie & Datumsbereich.
   * Pagination via `skip`/`take`, dazu Gesamtanzahl für UI.
   */
  // `MonaaLisa/src/Backend/src/papers/papers.service.ts`

  async findMany(q: QueryPaperDto) {
    const skip = Math.min(Math.max(0, toNumber(q.skip, 0)), MAX_SKIP);
    // Zweite Sicherung zusätzlich zur ValidationPipe: nie unbegrenzt laden.
    const take = Math.min(Math.max(1, toNumber(q.take, 20)), MAX_PAGE_SIZE);

    const where: Prisma.PaperWhereInput = {};

    // Stub-Titel grundsätzlich ausschließen (exakter Vergleich: billiger
    // Filter auf schon gefundenen Zeilen statt ILIKE)
    where.title = { not: STUB_TITLE };

    if (q.search && q.search.trim() !== '') {
      const term = q.search.trim();
      where.OR = [
        { title: { contains: term, mode: 'insensitive' } },
        { authors: { contains: term, mode: 'insensitive' } },
        { abstract: { contains: term, mode: 'insensitive' } },
      ];
    }

    if (q.categories) Object.assign(where, categoryWhere(q.categories));

    if (q.dateFrom || q.dateTo) {
      const publishedFilter: Prisma.DateTimeNullableFilter = {};
      if (q.dateFrom) publishedFilter.gte = new Date(q.dateFrom);
      if (q.dateTo) publishedFilter.lte = new Date(q.dateTo);
      where.published = publishedFilter;
    }

    // sort=citations nutzt die vorberechnete Spalte arxiv_citation_count
    // (indiziert) statt bei jeder Anfrage paper_citation zu zählen.
    // Immer mit eindeutigem Tiebreaker, sonst ist die Reihenfolge bei
    // gleichen Werten zufällig und Seiten überlappen.
    const orderBy: Prisma.PaperOrderByWithRelationInput[] =
      q.sort === 'citations'
        ? [{ arxiv_citation_count: 'desc' }, { id: 'asc' }]
        : [{ published: 'desc' }, { id: 'desc' }];

    const items = await this.prisma.paper.findMany({
      where,
      orderBy,
      skip,
      take,
    });

    // Gesamtanzahl: Ein exaktes COUNT über Millionen Zeilen kostet bei jeder
    // Anfrage Sekunden. Deshalb:
    // - letzte Seite schon erreicht → Anzahl ergibt sich direkt
    // - sonst höchstens COUNT_CAP+1 IDs holen (bricht früh ab) → "10000+"
    let total: number;
    let totalCapped = false;
    if (items.length < take) {
      total = skip + items.length;
    } else {
      const ids = await this.prisma.paper.findMany({
        where,
        select: { id: true },
        take: COUNT_CAP + 1,
      });
      totalCapped = ids.length > COUNT_CAP;
      total = Math.min(ids.length, COUNT_CAP);
    }

    // Zitationslisten nur für kleine Seiten anhängen (z. B. Sidebar-Samples)
    const shouldEnrich = take <= 50;

    const enrichedItems = shouldEnrich
      ? await this.enrichWithCitations(items)
      : items.map((p) => ({ ...p, citations: [], references: [] }));

    return { items: enrichedItems, total, totalCapped, skip, take };
  }

  /**
   * Punktwolke einer Kategorie für die Karte: nur ID, UMAP-Koordinaten und
   * Zitationszahlen – kein Abstract, keine Zitationslisten. Details lädt das
   * Frontend bei Hover/Klick über GET /papers/:entryId nach.
   *
   * Sortiert nach arXiv-Zitationen (absteigend), damit `limit` die
   * meistzitierten Paper liefert. Ohne `limit` kommt die ganze Kategorie.
   *
   * Liefert fertig serialisierte Bytes (JSON + gzip) aus dem PointsCache.
   */
  getPointsPayload(category: string, limit?: number): Promise<CachedPayload> {
    return this.pointsCache.get(`${category}|${limit ?? 'all'}`, () =>
      this.findPoints(category, limit),
    );
  }

  /**
   * Nutzt die generierten Spalten point_x/point_y. Für Präfix-Kategorien
   * ("cs.") deckt paper_points_prefix_idx die Abfrage komplett ab
   * (Index-Only-Scan – die breiten Zeilen mit Abstract werden nicht gelesen).
   */
  async findPoints(category: string, limit?: number): Promise<CategoryPoints> {
    const limitSql = limit ? Prisma.sql`LIMIT ${limit}` : Prisma.empty;

    const rows: PointRow[] = await this.prisma.$queryRaw`
      SELECT p.entry_id AS id,
             p.point_x AS x,
             p.point_y AS y,
             p.arxiv_citation_count AS cc,
             p.non_arxiv_citation_count AS nc
        FROM paper p
       WHERE ${categorySql(category)}
         AND p.point_x IS NOT NULL
         AND p.point_y IS NOT NULL
       ORDER BY p.arxiv_citation_count DESC, p.entry_id
       ${limitSql}
    `;

    const n = rows.length;
    const out: CategoryPoints = {
      category,
      count: n,
      ids: new Array<string>(n),
      x: new Array<number>(n),
      y: new Array<number>(n),
      cc: new Array<number>(n),
      nc: new Array<number>(n),
    };
    for (let i = 0; i < n; i++) {
      const r = rows[i];
      out.ids[i] = r.id;
      out.x[i] = round4(Number(r.x));
      out.y[i] = round4(Number(r.y));
      out.cc[i] = Number(r.cc);
      out.nc[i] = Number(r.nc ?? 0);
    }
    return out;
  }

  /**
   * IDs aller Paper einer Kategorie, deren Autorenfeld `author` enthält
   * (case-insensitive). Nutzt den Trigram-Index auf `authors` und den
   * GIN-Index auf `category_list`.
   * Liefert nur IDs, damit auch Autoren mit hunderten Papern vollständig
   * hervorgehoben werden können. Hartes Limit als Sicherheitsgurt.
   */
  async findAuthorPaperIds(
    category: string,
    author: string,
  ): Promise<{ ids: string[] }> {
    const rows: { entry_id: string }[] = await this.prisma.$queryRaw`
      SELECT p.entry_id
        FROM paper p
       WHERE ${categorySql(category)}
         AND p.authors ILIKE ${'%' + escapeLike(author.trim()) + '%'}
         AND p.point_x IS NOT NULL
       LIMIT ${AUTHOR_IDS_LIMIT}
    `;
    return { ids: rows.map((r) => r.entry_id) };
  }

  /**
   * Holt ein Paper per `entry_id`.
   * Wirft, wenn es die ID nicht gibt → bewusst, damit der Caller entscheiden kann.
   */
  async findByEntryId(entry_id: string) {
    // Alle drei Abfragen laufen über Primär-/Unique-Indizes. Die Listen sind
    // gedeckelt, damit stark zitierte Paper nicht bei jedem Hover MB laden.
    const [paper, citations, references] = await Promise.all([
      this.prisma.paper.findUniqueOrThrow({ where: { entry_id } }),
      this.prisma.paperCitation.findMany({
        where: { belonging_paper_entry_id: entry_id },
        select: { cited_paper_entry_id: true },
        take: MAX_INLINE_LINKS,
      }),
      this.prisma.paperReference.findMany({
        where: { belonging_paper_entry_id: entry_id },
        select: { referenced_paper_entry_id: true },
        take: MAX_INLINE_LINKS,
      }),
    ]);
    return {
      ...paper,
      citations: citations.map((c) => c.cited_paper_entry_id),
      references: references.map((r) => r.referenced_paper_entry_id),
    };
  }

  /**
   * Aktualisiert ein Paper per `entry_id`.
   * - Nur Felder überschreiben, die explizit kommen.
   * - `published`: wenn nicht gesetzt, bleibt der vorhandene Wert (via `undefined`)
   */
  async updateByEntryId(entry_id: string, dto: UpdatePaperDto) {
    this.pointsCache.clear(); // Punktwolken können sich geändert haben
    return this.prisma.paper.update({
      where: { entry_id },
      data: {
        title: dto.title,
        authors:
          dto.authors === undefined ? undefined : authorsToString(dto.authors),
        abstract: dto.abstract,
        categories: dto.categories,
        ...(dto.categories === undefined
          ? {}
          : deriveCategoryColumns(dto.categories)),
        published: dto.published ? new Date(dto.published) : undefined,
        updated: dto.updated ? new Date(dto.updated) : undefined,
        doi: dto.doi,
        journal_ref: dto.journal_ref,
        license: dto.license,
        url: dto.url,
        s2_id: dto.s2_id,
        non_arxiv_citation_count: dto.non_arxiv_citation_count,
        non_arxiv_reference_count: dto.non_arxiv_reference_count,
      },
    });
  }

  /**
   * Batch-Fetch: holt mehrere Papers anhand einer Liste von `entry_id`s.
   * Anzahl IDs ist im DTO auf MAX_BATCH_IDS begrenzt.
   */
  async findByEntryIds(entryIds: string[]) {
    if (!entryIds.length) return [];
    const papers = await this.prisma.paper.findMany({
      where: { entry_id: { in: [...new Set(entryIds)] } },
    });

    // Falls jemand 5000 IDs anfragt, NIEMALS enrichWithCitations machen
    if (entryIds.length > 50) {
      return papers.map((p) => ({ ...p, citations: [], references: [] }));
    }

    return this.enrichWithCitations(papers);
  }

  /**
   * Holt aus der paper_citation- und paper_reference-Tabelle die IDs für
   * jedes übergebene Paper und hängt sie als `citations` / `references` an.
   */
  private async enrichWithCitations<T extends { entry_id: string }>(
    papers: T[],
  ): Promise<(T & { citations: string[]; references: string[] })[]> {
    if (!papers.length) return [];

    const entryIds = papers.map((p) => p.entry_id);

    // Pro Paper höchstens LIST_LINKS_PER_PAPER Einträge (LATERAL + LIMIT).
    // Die Sidebar lädt gerade die meistzitierten Paper – ohne Deckel wären
    // das schnell Hunderttausende Zeilen für eine Liste mit 50 Einträgen.
    const [citRows, refRows] = await Promise.all([
      this.prisma.$queryRaw<
        { belonging_paper_entry_id: string; cited_paper_entry_id: string }[]
      >`
        SELECT b.entry_id AS belonging_paper_entry_id, l.cited_paper_entry_id
          FROM unnest(${entryIds}::text[]) AS b(entry_id)
          CROSS JOIN LATERAL (
            SELECT pc.cited_paper_entry_id
              FROM paper_citation pc
             WHERE pc.belonging_paper_entry_id = b.entry_id
             LIMIT ${LIST_LINKS_PER_PAPER}
          ) l
      `,
      this.prisma.$queryRaw<
        {
          belonging_paper_entry_id: string;
          referenced_paper_entry_id: string;
        }[]
      >`
        SELECT b.entry_id AS belonging_paper_entry_id, l.referenced_paper_entry_id
          FROM unnest(${entryIds}::text[]) AS b(entry_id)
          CROSS JOIN LATERAL (
            SELECT pr.referenced_paper_entry_id
              FROM paper_reference pr
             WHERE pr.belonging_paper_entry_id = b.entry_id
             LIMIT ${LIST_LINKS_PER_PAPER}
          ) l
      `,
    ]);

    const citationMap = new Map<string, string[]>();
    for (const r of citRows) {
      const arr = citationMap.get(r.belonging_paper_entry_id);
      if (arr) arr.push(r.cited_paper_entry_id);
      else
        citationMap.set(r.belonging_paper_entry_id, [r.cited_paper_entry_id]);
    }

    const referenceMap = new Map<string, string[]>();
    for (const r of refRows) {
      const arr = referenceMap.get(r.belonging_paper_entry_id);
      if (arr) arr.push(r.referenced_paper_entry_id);
      else
        referenceMap.set(r.belonging_paper_entry_id, [
          r.referenced_paper_entry_id,
        ]);
    }

    return papers.map((p) => ({
      ...p,
      citations: citationMap.get(p.entry_id) ?? [],
      references: referenceMap.get(p.entry_id) ?? [],
    }));
  }

  /**
   * Löscht ein Paper per `entry_id`.
   * Keine Magie – weg ist weg.
   */
  async deleteByEntryId(entry_id: string) {
    this.pointsCache.clear(); // Punktwolken können sich geändert haben
    return this.prisma.paper.delete({ where: { entry_id } });
  }
}

const AUTHOR_IDS_LIMIT = 20000;

/** Max. Zitationen/Referenzen pro Paper in Listenansichten (enrichWithCitations). */
const LIST_LINKS_PER_PAPER = 1000;

/**
 * Kategorie-Filter:
 * - endet auf "." (z. B. "cs.") → Präfix auf primary_category
 * - sonst exakte Kategorie (z. B. "cs.CL", "hep-ph") → category_list enthält sie
 */
function isPrefixCategory(category: string): boolean {
  return category.endsWith('.');
}

function categoryWhere(category: string): Prisma.PaperWhereInput {
  return isPrefixCategory(category)
    ? { primary_category: { startsWith: category } }
    : { category_list: { has: category } };
}

function categorySql(category: string): Prisma.Sql {
  return isPrefixCategory(category)
    ? Prisma.sql`p.primary_category LIKE ${escapeLike(category) + '%'}`
    : Prisma.sql`p.category_list @> ARRAY[${category}]::text[]`;
}

/** Leitet category_list/primary_category aus dem Kategorie-String ab. */
function deriveCategoryColumns(categories: string | null | undefined): {
  category_list: string[];
  primary_category: string | null;
} {
  const list = (categories ?? '').split(/[\s,]+/).filter(Boolean);
  return { category_list: list, primary_category: list[0] ?? null };
}

/** Maskiert LIKE-Platzhalter (% und _) in Nutzereingaben. */
function escapeLike(value: string): string {
  return value.replace(/[\\%_]/g, (c) => '\\' + c);
}

/**
 *
 * Konvertiert einen unbekannten Wert in eine endliche Zahl, wobei der angegebene Fallback zurückgegeben wird, wenn die Konvertierung nicht möglich ist.
 */
function toNumber(value: unknown, fallback: number): number {
  const parsed =
    typeof value === 'string' && value.trim() !== ''
      ? Number(value)
      : typeof value === 'number'
        ? value
        : NaN;

  if (Number.isFinite(parsed)) {
    return parsed;
  }
  return fallback;
}

/**
 * Wandelt ein array von authors (string[]) in einen einzelnen String um, welcher im Frontend dann angezeigt werden kann
 * @param authors Array von Autoren
 * @returns Autoren konkateniert
 */
function authorsToString(
  authors: string[] | string | null | undefined,
): string {
  if (authors == null) return '';
  return Array.isArray(authors) ? authors.join(', ') : authors;
}
