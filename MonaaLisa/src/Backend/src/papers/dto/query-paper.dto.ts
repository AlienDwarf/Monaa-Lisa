import { Type } from 'class-transformer';
import {
  ArrayMaxSize,
  IsArray,
  IsInt,
  IsNotEmpty,
  IsOptional,
  IsString,
  Max,
  MaxLength,
  Min,
  MinLength,
  IsISO8601,
  ValidateNested,
} from 'class-validator';

/** Obergrenze für Endpoints, die volle Paper-Objekte (inkl. Abstract) liefern. */
export const MAX_PAGE_SIZE = 1000;

/** Tiefer als das blättert niemand – und OFFSET muss alle Zeilen davor lesen. */
export const MAX_SKIP = 10_000;

/** Zählen über Millionen Zeilen ist teuer → ab hier nur noch "10000+". */
export const COUNT_CAP = 10_000;

/** Max. IDs pro POST /papers/batch (Frontend schickt 500er-Blöcke). */
export const MAX_BATCH_IDS = 1_000;

/** Max. IDs pro POST /papers/neighbours/filtered (bleibt unter dem pg-Parameterlimit). */
export const MAX_NEIGHBOUR_IDS = 20_000;

/** Max. Zitationen/Referenzen pro Abfrage eines Papers. */
export const MAX_LINKS = 50_000;

export class QueryPaperDto {
  // Volltextsuche über title/authors/abstract. Mindestens 3 Zeichen, weil die
  // Trigram-Indizes kürzere Begriffe nicht abdecken (→ Full-Table-Scan).
  @IsOptional()
  @IsString()
  @MinLength(3)
  @MaxLength(200)
  search?: string;

  @IsOptional()
  @IsString()
  @MaxLength(100)
  categories?: string;

  // Published-Zeitfenster (im Service in Date umwandeln)
  @IsOptional()
  @IsISO8601()
  dateFrom?: string;

  @IsOptional()
  @IsISO8601()
  dateTo?: string;

  // Sort order: 'published' (default) or 'citations' (most cited first)
  @IsOptional()
  @IsString()
  sort?: string;

  // Pagination
  @IsOptional()
  @Type(() => Number)
  @IsInt()
  @Min(0)
  @Max(MAX_SKIP)
  skip: number = 0;

  // Gedeckelt: volle Paper-Objekte sind 1–2 KB groß. Für große Mengen
  // (ganze Kategorien) gibt es GET /papers/points.
  @IsOptional()
  @Type(() => Number)
  @IsInt()
  @Min(1)
  @Max(MAX_PAGE_SIZE)
  take: number = 20;
}

/**
 * Query für GET /papers/points – schlanke Punktwolke einer Kategorie.
 * Ohne `limit` werden alle Paper der Kategorie geliefert ("All").
 */
export class PointsQueryDto {
  @IsString()
  @IsNotEmpty()
  @MaxLength(100)
  category!: string;

  @IsOptional()
  @Type(() => Number)
  @IsInt()
  @Min(1)
  @Max(5_000_000)
  limit?: number;
}

/** Body für POST /papers/batch */
export class BatchPapersDto {
  @IsArray()
  @ArrayMaxSize(MAX_BATCH_IDS)
  @IsString({ each: true })
  @MaxLength(100, { each: true })
  entryIds: string[] = [];
}

/** Filter für POST /papers/neighbours/filtered */
export class NeighbourFilterDto {
  @IsOptional()
  @IsISO8601()
  dateFrom?: string;

  @IsOptional()
  @IsISO8601()
  dateTo?: string;

  @IsOptional()
  @IsString()
  @MaxLength(200)
  authorQuery?: string;

  @IsOptional()
  @IsString()
  @MaxLength(200)
  abstractQuery?: string;

  @IsOptional()
  @IsString()
  @MaxLength(200)
  titleQuery?: string;

  @IsOptional()
  @Type(() => Number)
  @IsInt()
  @Min(0)
  minCitations?: number;
}

/** Body für POST /papers/neighbours/filtered */
export class FilteredNeighboursDto {
  @IsArray()
  @ArrayMaxSize(MAX_NEIGHBOUR_IDS)
  @IsString({ each: true })
  @MaxLength(100, { each: true })
  entryIds: string[] = [];

  @IsOptional()
  @ValidateNested()
  @Type(() => NeighbourFilterDto)
  filter: NeighbourFilterDto = new NeighbourFilterDto();
}

/** Query für GET /paper-citations/paper/:id und /paper-references/paper/:id */
export class LinksQueryDto {
  @IsOptional()
  @Type(() => Number)
  @IsInt()
  @Min(1)
  @Max(MAX_LINKS)
  limit: number = MAX_LINKS;
}

/** Query für GET /papers/points/ids – IDs aller Paper eines Autors in einer Kategorie. */
export class AuthorIdsQueryDto {
  @IsString()
  @IsNotEmpty()
  category!: string;

  // Mindestens 3 Zeichen: kürzere Begriffe kann der Trigram-Index nicht nutzen
  @IsString()
  @MinLength(3)
  @MaxLength(200)
  author!: string;
}
