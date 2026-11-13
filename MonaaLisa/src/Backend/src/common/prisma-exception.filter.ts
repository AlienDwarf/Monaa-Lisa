import {
  ArgumentsHost,
  Catch,
  ConflictException,
  NotFoundException,
  BadRequestException,
} from '@nestjs/common';
import { BaseExceptionFilter } from '@nestjs/core';
import { Prisma } from '@prisma/client';

/**
 * Übersetzt bekannte Prisma-Fehler in passende HTTP-Statuscodes statt 500:
 * - P2025 (Datensatz nicht gefunden) → 404, z. B. GET /papers/<unbekannte ID>
 * - P2002 (Unique-Verletzung)        → 409
 * - P2000 (Wert zu lang)             → 400
 * Alles andere läuft wie gehabt über Nests Standard-Handler.
 */
@Catch(Prisma.PrismaClientKnownRequestError)
export class PrismaExceptionFilter extends BaseExceptionFilter {
  catch(exception: Prisma.PrismaClientKnownRequestError, host: ArgumentsHost) {
    switch (exception.code) {
      case 'P2025':
        return super.catch(new NotFoundException('Paper not found'), host);
      case 'P2002':
        return super.catch(new ConflictException('Already exists'), host);
      case 'P2000':
        return super.catch(new BadRequestException('Value too long'), host);
      default:
        return super.catch(exception, host);
    }
  }
}
