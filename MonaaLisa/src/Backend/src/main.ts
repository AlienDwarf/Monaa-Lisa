/**
 * Nico August 2025
 */
import { ValidationPipe } from '@nestjs/common';
import { HttpAdapterHost, NestFactory } from '@nestjs/core';
import compression from 'compression';
import { AppModule } from './app.module';
import { PrismaExceptionFilter } from './common/prisma-exception.filter';

type EnvAccessor = {
  process?: {
    env?: Record<string, string | undefined>;
  };
};

function readEnv() {
  return (globalThis as EnvAccessor).process?.env ?? {};
}

async function bootstrap() {
  const app = await NestFactory.create(AppModule);
  const env = readEnv();
  const port = Number(env.PORT ?? 3000);
  const allowedOrigins = (env.FRONTEND_ORIGIN ?? 'http://localhost:5173')
    .split(',')
    .map((origin) => origin.trim())
    .filter(Boolean);

  // Ohne diese Pipe werden die class-validator-Dekoratoren der DTOs
  // (z. B. @Max auf `take`) und deren Defaults nie angewendet.
  app.useGlobalPipes(
    new ValidationPipe({
      transform: true,
      whitelist: true,
    }),
  );

  // Prisma-Fehler wie "nicht gefunden" als 404 statt 500
  app.useGlobalFilters(
    new PrismaExceptionFilter(app.get(HttpAdapterHost).httpAdapter),
  );

  // Sauber herunterfahren (offene DB-Verbindungen schließen) bei SIGTERM
  app.enableShutdownHooks();

  // gzip für JSON-Antworten – die Punktwolke ganzer Kategorien schrumpft
  // dadurch auf etwa ein Drittel.
  app.use(compression());

  app.enableCors({
    origin(origin, callback) {
      if (!origin || allowedOrigins.includes(origin)) {
        callback(null, true);
        return;
      }
      callback(new Error(`Origin ${origin} not allowed by CORS`));
    },
    credentials: true,
  });

  await app.listen(port);
}
void bootstrap();
