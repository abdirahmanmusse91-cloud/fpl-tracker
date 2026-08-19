"""Databasmigrationer som körs automatiskt när backend startar.

Kräver miljövariabeln DATABASE_URL. Den hittas i Supabase under
Settings → Database → Connection string → "Session pooler" (välj URI och
byt ut [YOUR-PASSWORD] mot databaslösenordet). Sätts den en gång i Render
behöver inga ALTER TABLE köras för hand igen — nya migrationer läggs bara
till i listan nedan och kör vid nästa deploy.

Saknas DATABASE_URL hoppas migreringen över utan att appen påverkas;
schemavakten i main.py stänger då av gw_cache tills kolumnerna finns.

Varje migration körs exakt en gång och spåras i public.schema_migrations.
Skriv dem idempotenta ändå — det gör dem ofarliga att köra om för hand.
"""

import os

TRACKING_TABLE = """
CREATE TABLE IF NOT EXISTS public.schema_migrations (
  name       TEXT PRIMARY KEY,
  applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

MIGRATIONS: list[tuple[str, str]] = [
    # Kolumnerna som MINUS/BÄNK-flikarna och final-logiken behöver.
    ("001_cache_columns", """
        ALTER TABLE public.gw_cache
          ADD COLUMN IF NOT EXISTS points_on_bench INTEGER NOT NULL DEFAULT 0,
          ADD COLUMN IF NOT EXISTS points_on_hits  INTEGER NOT NULL DEFAULT 0,
          ADD COLUMN IF NOT EXISTS final           BOOLEAN NOT NULL DEFAULT false;
    """),

    # gw_cache saknade säsong, så förra säsongens GW1 och den nya säsongens GW1
    # var samma rad. Med säsongen i primärnyckeln kan de aldrig krocka, och
    # ingen manuell rensning behövs inför en ny säsong.
    # Befintliga rader är från 25/26 och får därför season = 2025.
    ("002_season_key", """
        ALTER TABLE public.gw_cache
          ADD COLUMN IF NOT EXISTS season INTEGER NOT NULL DEFAULT 2025;

        DO $$
        DECLARE pk_name text;
        BEGIN
          SELECT conname INTO pk_name
            FROM pg_constraint
           WHERE conrelid = 'public.gw_cache'::regclass AND contype = 'p';
          IF pk_name IS NOT NULL THEN
            EXECUTE format('ALTER TABLE public.gw_cache DROP CONSTRAINT %I', pk_name);
          END IF;
        END $$;

        ALTER TABLE public.gw_cache ADD PRIMARY KEY (entry_id, season, gw);

        ALTER TABLE public.gw_cache ALTER COLUMN season DROP DEFAULT;
    """),
]


async def run_migrations() -> str:
    """Kör väntande migrationer. Returnerar en rad att logga."""
    url = os.environ.get("DATABASE_URL")
    if not url:
        return "DATABASE_URL saknas — hoppar över migrationer"

    try:
        import asyncpg
    except ImportError:
        return "asyncpg saknas — hoppar över migrationer"

    conn = await asyncpg.connect(url, timeout=20)
    try:
        await conn.execute(TRACKING_TABLE)
        done = {r["name"] for r in await conn.fetch("SELECT name FROM public.schema_migrations")}

        applied = []
        for name, sql in MIGRATIONS:
            if name in done:
                continue
            async with conn.transaction():
                await conn.execute(sql)
                await conn.execute(
                    "INSERT INTO public.schema_migrations (name) VALUES ($1)", name
                )
            applied.append(name)

        if applied:
            return f"migrationer körda: {', '.join(applied)}"
        return f"schemat är aktuellt ({len(done)} migrationer sedan tidigare)"
    finally:
        await conn.close()
