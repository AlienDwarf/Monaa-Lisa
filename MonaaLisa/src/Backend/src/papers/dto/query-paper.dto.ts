import { Type } from 'class-transformer';
import {
  IsInt,
  IsNotEmpty,
  IsOptional,
  IsString,
  Max,
  MaxLength,
  Min,
  MinLength,
  IsISO8601,
} from 'class-validator';

/** Obergrenze für Endpoints, die volle Paper-Objekte (inkl. Abstract) liefern. */
export const MAX_PAGE_SIZE = 1000;

export class QueryPaperDto {
  // Volltextsuche über title/authors/summary
  @IsOptional()
  @IsString()
  search?: string;

  @IsOptional()
  @IsString()
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
  category!: string;

  @IsOptional()
  @Type(() => Number)
  @IsInt()
  @Min(1)
  limit?: number;
}

/** Query für GET /papers/points/ids – IDs aller Paper eines Autors in einer Kategorie. */
export class AuthorIdsQueryDto {
  @IsString()
  @IsNotEmpty()
  category!: string;

  // Mindestens 2 Zeichen, sonst trifft die Suche fast jedes Paper
  @IsString()
  @MinLength(2)
  @MaxLength(200)
  author!: string;
}
