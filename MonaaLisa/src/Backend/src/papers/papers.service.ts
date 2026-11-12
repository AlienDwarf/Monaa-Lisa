import { Injectable } from '@nestjs/common';
import { PrismaService } from '../prisma/prisma.service';
import { Prisma } from '@prisma/client';
import { CreatePaperDto } from './dto/create-paper.dto';
import { UpdatePaperDto } from './dto/update-paper.dto';
import { MAX_PAGE_SIZE, QueryPaperDto } from './dto/query-paper.dto';

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

type PointRow = { id: string; x: number; y: number; cc: number; nc: number };

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

  /**
   * Legt ein neues Paper an.
   * - `published` ist optional → wenn nicht da, speichern wir `null`
   * - `added` ist der Zeitpunkt, zu dem wir es in unser System aufnehmen
   */
  async create(dto: CreatePaperDto) {
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
    filter: {
      dateFrom?: string;
      dateTo?: string;
      authorQuery?: string;
      abstractQuery?: string;
      minCitations?: number;
      // onlyArxiv wird implizit behandelt, da non-arXiv Paper
      // oft eh nicht in der Haupt-Tabelle liegen oder keine validen Daten haben.
    },
  ) {
    if (!entryIds || entryIds.length === 0) return [];

    const where: Prisma.PaperWhereInput = {
      entry_id: { in: entryIds },
    };

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
    const skip = Math.max(0, toNumber(q.skip, 0));
    // Zweite Sicherung zusätzlich zur ValidationPipe: nie unbegrenzt laden.
    const take = Math.min(Math.max(1, toNumber(q.take, 20)), MAX_PAGE_SIZE);

    const where: Prisma.PaperWhereInput = {};

    // Stub-Titel grundsätzlich ausschließen
    where.NOT = [
      { title: { equals: '[STUB] Pending Fetch', mode: 'insensitive' } },
    ];

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
    const orderBy: Prisma.PaperOrderByWithRelationInput[] =
      q.sort === 'citations'
        ? [{ arxiv_citation_count: 'desc' }, { id: 'asc' }]
        : [{ published: 'desc' }];

    const [items, total] = await Promise.all([
      this.prisma.paper.findMany({ where, orderBy, skip, take }),
      this.prisma.paper.count({ where }),
    ]);

    // Zitationslisten nur für kleine Seiten anhängen (z. B. Sidebar-Samples)
    const shouldEnrich = take <= 50;

    const enrichedItems = shouldEnrich
      ? await this.enrichWithCitations(items)
      : items.map((p) => ({ ...p, citations: [], references: [] }));

    return { items: enrichedItems, total, skip, take };
  }

  /**
   * Punktwolke einer Kategorie für die Karte: nur ID, UMAP-Koordinaten und
   * Zitationszahlen – kein Abstract, keine Zitationslisten. Details lädt das
   * Frontend bei Hover/Klick über GET /papers/:entryId nach.
   *
   * Sortiert nach arXiv-Zitationen (absteigend), damit `limit` die
   * meistzitierten Paper liefert. Ohne `limit` kommt die ganze Kategorie.
   */
  async findPoints(category: string, limit?: number): Promise<CategoryPoints> {
    const limitSql = limit ? Prisma.sql`LIMIT ${limit}` : Prisma.empty;

    const rows: PointRow[] = await this.prisma.$queryRaw`
      SELECT p.entry_id AS id,
             ROUND((p.tsne::jsonb->>'x')::numeric, 4)::float8 AS x,
             ROUND((p.tsne::jsonb->>'y')::numeric, 4)::float8 AS y,
             p.arxiv_citation_count AS cc,
             COALESCE(p.non_arxiv_citation_count, 0)::int AS nc
        FROM paper p
       WHERE ${categorySql(category)}
         AND jsonb_typeof(p.tsne::jsonb->'x') = 'number'
         AND jsonb_typeof(p.tsne::jsonb->'y') = 'number'
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
      out.x[i] = Number(r.x);
      out.y[i] = Number(r.y);
      out.cc[i] = Number(r.cc);
      out.nc[i] = Number(r.nc);
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
         AND p.tsne IS NOT NULL
       LIMIT ${AUTHOR_IDS_LIMIT}
    `;
    return { ids: rows.map((r) => r.entry_id) };
  }

  /**
   * Holt ein Paper per `entry_id`.
   * Wirft, wenn es die ID nicht gibt → bewusst, damit der Caller entscheiden kann.
   */
  async findByEntryId(entry_id: string) {
    const paper = await this.prisma.paper.findUniqueOrThrow({
      where: { entry_id },
    });
    const [citations, references] = await Promise.all([
      this.prisma.paperCitation.findMany({
        where: { belonging_paper_entry_id: entry_id },
        select: { cited_paper_entry_id: true },
      }),
      this.prisma.paperReference.findMany({
        where: { belonging_paper_entry_id: entry_id },
        select: { referenced_paper_entry_id: true },
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
   * Maximal 5000 Ergebnisse.
   */
  async findByEntryIds(entryIds: string[], take = 5000) {
    const papers = await this.prisma.paper.findMany({
      where: { entry_id: { in: entryIds } },
      take,
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

    const [citRows, refRows] = await Promise.all([
      this.prisma.paperCitation.findMany({
        where: { belonging_paper_entry_id: { in: entryIds } },
        select: { belonging_paper_entry_id: true, cited_paper_entry_id: true },
      }),
      this.prisma.paperReference.findMany({
        where: { belonging_paper_entry_id: { in: entryIds } },
        select: {
          belonging_paper_entry_id: true,
          referenced_paper_entry_id: true,
        },
      }),
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
    return this.prisma.paper.delete({ where: { entry_id } });
  }
}

const AUTHOR_IDS_LIMIT = 20000;

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
