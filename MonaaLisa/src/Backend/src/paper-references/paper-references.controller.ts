import { Controller, Get, Param, Query } from '@nestjs/common';
import { LinksQueryDto } from '../papers/dto/query-paper.dto';
import { PaperReferencesService } from './paper-references.service';

@Controller('paper-references')
export class PaperReferencesController {
  constructor(
    private readonly paperReferencesService: PaperReferencesService,
  ) {}

  @Get('paper/:entryId')
  findByBelongingPaper(
    @Param('entryId') entryId: string,
    @Query() q: LinksQueryDto,
  ) {
    return this.paperReferencesService.findByBelongingPaper(entryId, q.limit);
  }
}
