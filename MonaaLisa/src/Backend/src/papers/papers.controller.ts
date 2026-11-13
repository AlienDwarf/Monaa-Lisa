/**
 * Nico - August 2025
 *
 * PapersController
 * -------------------------------
 * REST-Endpunkte rund um "Paper".
 * Der Controller ist schlank gehalten und delegiert alles an den Service.
 * Validierung/Transformation passiert über die DTOs (class-validator / class-transformer).
 */
import {
  Body,
  Controller,
  Delete,
  Get,
  Header,
  Param,
  Patch,
  Post,
  Query,
  Req,
  Res,
  UseGuards,
} from '@nestjs/common';
import type { Request, Response } from 'express';
import { AdminTokenGuard } from '../auth/admin-token.guard';
import { PapersService } from './papers.service';
import { CreatePaperDto } from './dto/create-paper.dto';
import { UpdatePaperDto } from './dto/update-paper.dto';
import {
  AuthorIdsQueryDto,
  BatchPapersDto,
  FilteredNeighboursDto,
  PointsQueryDto,
  QueryPaperDto,
} from './dto/query-paper.dto';

@Controller('papers')
export class PapersController {
  constructor(private readonly papers: PapersService) {}

  /**
   * POST /papers
   * Legt ein neues Paper an.
   *
   * Erwartet: CreatePaperDto im Body.
   * Hinweis: Standard-HTTP-Code für POST ist 201 (Created), sofern nicht überschrieben.
   */
  @Post()
  @UseGuards(AdminTokenGuard) // Authorization: Bearer <ADMIN_TOKEN>
  create(@Body() dto: CreatePaperDto) {
    return this.papers.create(dto);
  }

  /**
   * POST /papers/upsert
   * Idempotente Ingestion: legt an oder aktualisiert anhand `entry_id`.
   *
   * Use-Case: Feeds/Importer, die dasselbe Item mehrfach sehen können.
   */
  @Post('upsert')
  @UseGuards(AdminTokenGuard) // Authorization: Bearer <ADMIN_TOKEN>
  upsert(@Body() dto: CreatePaperDto) {
    return this.papers.upsertByEntryId(dto);
  }

  /**
   * POST /papers/batch
   * Batch-Fetch: holt mehrere Papers anhand einer Liste von `entry_id`s.
   */
  @Post('batch')
  findBatch(@Body() body: BatchPapersDto) {
    return this.papers.findByEntryIds(body.entryIds);
  }

  // Nick - November 2025
  // Die Möglichkeiten mehrere Papers zu finden und anschließend im FE zu filtern soll gegeben sein
  /**
   * GET /papers
   * Listet Papers mit Suche, Filtern und Pagination.
   *
   * Query-Parameter (QueryPaperDto):
   * - search: Volltext-like über title/authors/summary
   * - category: exakter Match
   * - dateFrom/dateTo: Datumsbereich (published)
   * - skip/take: Pagination
   *
   * Rückgabe: { items, total, skip, take }
   */
  @Get()
  findMany(@Query() q: QueryPaperDto) {
    return this.papers.findMany(q);
  }

  /**
   * GET /papers/points?category=cs.LG[&limit=1000]
   * Schlanke Punktwolke (ID, x, y, Zitationszahlen) einer ganzen Kategorie
   * für die Kartenansicht. Ohne `limit` = alle Paper der Kategorie.
   *
   * Muss VOR `:entryId` stehen, sonst wird "points" als entryId gematcht.
   * Daten ändern sich nur beim (monatlichen) Pipeline-Update → cachebar.
   */
  @Get('points')
  async findPoints(
    @Query() q: PointsQueryDto,
    @Req() req: Request,
    @Res() res: Response,
  ) {
    // Fertige Bytes aus dem Server-Cache: kein erneutes Abfragen,
    // Serialisieren oder Komprimieren pro Request.
    const payload = await this.papers.getPointsPayload(q.category, q.limit);
    res.setHeader('Content-Type', 'application/json; charset=utf-8');
    res.setHeader('Cache-Control', 'public, max-age=3600');
    res.setHeader('Vary', 'Accept-Encoding');
    if (/\bgzip\b/.test(req.headers['accept-encoding'] ?? '')) {
      // Content-Encoding gesetzt → compression-Middleware komprimiert nicht erneut
      res.setHeader('Content-Encoding', 'gzip');
      res.setHeader('Content-Length', payload.gz.length);
      res.end(payload.gz);
    } else {
      res.end(payload.json);
    }
  }

  /**
   * GET /papers/points/ids?category=cs.LG&author=Hinton
   * IDs aller Paper der Kategorie mit passendem Autor – für die
   * Autoren-Hervorhebung in der Karte (die Punkte kennen keine Autoren).
   */
  @Get('points/ids')
  @Header('Cache-Control', 'public, max-age=3600')
  findAuthorPaperIds(@Query() q: AuthorIdsQueryDto) {
    return this.papers.findAuthorPaperIds(q.category, q.author);
  }

  /**
   * GET /papers/:entryId
   * Holt ein einzelnes Paper anhand der externen `entryId`.
   * Achtung: Das ist die externe ID (z.B. aus dem Feed), nicht die DB-Primär-ID.
   */
  @Get(':entryId')
  findOne(@Param('entryId') entryId: string) {
    return this.papers.findByEntryId(entryId);
  }

  /**
   * PATCH /papers/:entryId
   * Aktualisiert Felder eines Papers.
   *
   * Verhalten:
   * - Nur übergebene Felder werden geändert.
   * - `published` wird nur gesetzt, wenn im DTO vorhanden.
   */
  @Patch(':entryId')
  @UseGuards(AdminTokenGuard) // Authorization: Bearer <ADMIN_TOKEN>
  update(@Param('entryId') entryId: string, @Body() dto: UpdatePaperDto) {
    return this.papers.updateByEntryId(entryId, dto);
  }

  @Post('neighbours/filtered')
  async getNeighbours(@Body() body: FilteredNeighboursDto) {
    return this.papers.findFilteredNeighbours(body.entryIds, body.filter);
  }

  // Nick - November 2025
  // Die Möglichkeiten ein paper zu löschen damit es neugeladen werden kann
  /**
   * DELETE /papers/:entryId
   * Löscht ein Paper.
   */
  @Delete(':entryId')
  @UseGuards(AdminTokenGuard) // Authorization: Bearer <ADMIN_TOKEN>
  remove(@Param('entryId') entryId: string) {
    return this.papers.deleteByEntryId(entryId);
  }
}
