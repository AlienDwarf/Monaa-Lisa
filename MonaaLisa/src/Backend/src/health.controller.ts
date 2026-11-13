import { Controller, Get, ServiceUnavailableException } from '@nestjs/common';
import { PrismaService } from './prisma/prisma.service';

/**
 * GET /health – für den Docker-Healthcheck.
 * Prüft auch die Datenbank: Ohne DB ist das Backend nicht "gesund",
 * auch wenn der HTTP-Server läuft.
 */
@Controller('health')
export class HealthController {
  constructor(private readonly prisma: PrismaService) {}

  @Get()
  async check() {
    try {
      await this.prisma.$queryRaw`SELECT 1`;
      return { status: 'ok' };
    } catch {
      throw new ServiceUnavailableException({ status: 'db_unreachable' });
    }
  }
}
