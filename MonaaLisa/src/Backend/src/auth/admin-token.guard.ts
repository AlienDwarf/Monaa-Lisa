import {
  CanActivate,
  ExecutionContext,
  ForbiddenException,
  Injectable,
  UnauthorizedException,
} from '@nestjs/common';
import { timingSafeEqual } from 'crypto';
import type { Request } from 'express';

/**
 * Schützt schreibende Endpoints (Anlegen, Ändern, Löschen von Papern).
 *
 * Erwartet den Standard-Header `Authorization: Bearer <ADMIN_TOKEN>`.
 * Ist ADMIN_TOKEN nicht gesetzt, sind die Endpoints komplett gesperrt –
 * so ist der sichere Zustand der Standard.
 */
@Injectable()
export class AdminTokenGuard implements CanActivate {
  canActivate(context: ExecutionContext): boolean {
    const expected = process.env.ADMIN_TOKEN?.trim();
    if (!expected) {
      throw new ForbiddenException(
        'Write endpoints are disabled (ADMIN_TOKEN not configured)',
      );
    }

    const req = context.switchToHttp().getRequest<Request>();
    const header = req.headers.authorization ?? '';
    // Schema ist laut RFC case-insensitive ("Bearer", "bearer", …)
    const match = /^Bearer\s+(.+)$/i.exec(header);
    if (!match) {
      throw new UnauthorizedException('Missing Bearer token');
    }
    if (!safeEqual(match[1].trim(), expected)) {
      throw new ForbiddenException('Invalid admin token');
    }
    return true;
  }
}

function safeEqual(a: string, b: string): boolean {
  const ab = Buffer.from(a);
  const bb = Buffer.from(b);
  return ab.length === bb.length && timingSafeEqual(ab, bb);
}
