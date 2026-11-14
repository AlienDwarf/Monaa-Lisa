import { Injectable } from '@nestjs/common';
import { PrismaService } from '../prisma/prisma.service';
import { MAX_LINKS } from '../papers/dto/query-paper.dto';
import { STUB_TITLE } from '../papers/papers.service';

@Injectable()
export class PaperCitationsService {
  constructor(private prisma: PrismaService) {}

  /**
   * Zitationen eines Papers – ohne Stub-Paper, meistzitierte zuerst.
   *
   * Eine einzige Abfrage (JOIN über den Primärschlüssel von paper_citation und den
   * Unique-Index auf paper.entry_id) statt zwei Abfragen mit riesiger
   * IN-Liste. `limit` deckelt stark zitierte Paper.
   */
  async findByBelongingPaper(entryId: string, limit: number = MAX_LINKS) {
    return this.prisma.$queryRaw<
      { belonging_paper_entry_id: string; cited_paper_entry_id: string }[]
    >`
      SELECT l.belonging_paper_entry_id, l.cited_paper_entry_id
        FROM paper_citation l
        JOIN paper p ON p.entry_id = l.cited_paper_entry_id
       WHERE l.belonging_paper_entry_id = ${entryId}
         AND p.title <> ${STUB_TITLE}
       ORDER BY p.arxiv_citation_count DESC, l.cited_paper_entry_id
       LIMIT ${Math.min(Math.max(1, limit), MAX_LINKS)}
    `;
  }
}
