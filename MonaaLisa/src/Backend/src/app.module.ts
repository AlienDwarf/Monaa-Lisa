/**
 * Nico August 2025
 */
import { Module } from '@nestjs/common';
import { AppController } from './app.controller';
import { AppService } from './app.service';
import { HealthController } from './health.controller';
import { PrismaModule } from './prisma/prisma.module';
import { PapersModule } from './papers/papers.module';
import { PaperCitationsModule } from './paper-citations/paper-citations.module';
import { PaperReferencesModule } from './paper-references/paper-references.module';

// Das ist unser Modul. Module enthalten Struktur aber keinen Code selbst.
// Der typische ablauf ist Client->Controller->Service->(DB)->Service->Controller->Client
@Module({
  imports: [
    PrismaModule, // @Global – stellt PrismaService einmal für alle Module bereit
    PapersModule,
    PaperCitationsModule,
    PaperReferencesModule,
  ], // andere @Module die intern benötigt werden
  // HealthController liefert GET /health (inkl. DB-Check) für Docker
  controllers: [AppController, HealthController],
  // PrismaService/PapersService hier NICHT erneut eintragen: das würde eine
  // zweite PrismaClient-Instanz mit eigenem Connection-Pool erzeugen.
  providers: [AppService],
})
export class AppModule {}
