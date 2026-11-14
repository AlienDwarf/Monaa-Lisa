/**
 * Nico August 2025
 */
import { Injectable, OnModuleInit, OnModuleDestroy } from '@nestjs/common';
import { PrismaClient } from '@prisma/client';
import { Pool } from 'pg';
import { PrismaPg } from '@prisma/adapter-pg';

function envInt(name: string, fallback: number): number {
  const v = Number(process.env[name]);
  return Number.isFinite(v) && v > 0 ? v : fallback;
}

@Injectable()
export class PrismaService
  extends PrismaClient
  implements OnModuleInit, OnModuleDestroy
{
  constructor() {
    // Ohne Limits wartet ein Request ewig auf eine Verbindung, sobald ein paar
    // langsame Abfragen den Pool belegen. Mit Timeouts schlägt er stattdessen
    // nach kurzer Zeit fehl und die Verbindung wird wieder frei.
    const pool = new Pool({
      connectionString: process.env.DATABASE_URL,
      max: envInt('DB_POOL_MAX', 20),
      // max. Wartezeit auf eine freie Verbindung
      connectionTimeoutMillis: envInt('DB_CONNECT_TIMEOUT_MS', 5_000),
      idleTimeoutMillis: 30_000,
      // serverseitig: Postgres bricht Abfragen nach dieser Zeit ab
      statement_timeout: envInt('DB_STATEMENT_TIMEOUT_MS', 30_000),
      // clientseitig: Sicherheitsnetz, falls der Server nicht antwortet
      query_timeout: envInt('DB_STATEMENT_TIMEOUT_MS', 30_000) + 5_000,
      idle_in_transaction_session_timeout: 60_000,
    });
    const adapter = new PrismaPg(pool);
    super({ adapter });
  }

  async onModuleInit() {
    await this.$connect();
  }

  async onModuleDestroy() {
    await this.$disconnect();
  }
}
