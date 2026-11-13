import { Controller, Get, Param, Query } from '@nestjs/common';
import { LinksQueryDto } from '../papers/dto/query-paper.dto';
import { PaperCitationsService } from './paper-citations.service';

@Controller('paper-citations')
export class PaperCitationsController {
  constructor(private readonly paperCitationsService: PaperCitationsService) {}

  @Get('paper/:entryId')
  findByBelongingPaper(
    @Param('entryId') entryId: string,
    @Query() q: LinksQueryDto,
  ) {
    return this.paperCitationsService.findByBelongingPaper(entryId, q.limit);
  }
}
