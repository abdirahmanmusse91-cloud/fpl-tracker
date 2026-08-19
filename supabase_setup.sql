-- Run this once in the Supabase SQL Editor:
-- https://supabase.com/dashboard/project/gduuhbvkyjcesahmooon/sql/new

CREATE TABLE IF NOT EXISTS public.gw_cache (
  entry_id  INTEGER  NOT NULL,
  gw        INTEGER  NOT NULL,
  points    INTEGER  NOT NULL,
  synced_at TIMESTAMPTZ DEFAULT now(),
  PRIMARY KEY (entry_id, gw)
);

-- Allow public read/write (no auth needed from the backend)
ALTER TABLE public.gw_cache ENABLE ROW LEVEL SECURITY;

CREATE POLICY "allow all" ON public.gw_cache
  FOR ALL USING (true) WITH CHECK (true);

-- ── Schemaändringar ──
-- Sköts numera automatiskt av migrations.py vid uppstart. Sätt DATABASE_URL
-- (Supabase → Settings → Database → Connection string → Session pooler) så
-- körs väntande migrationer vid varje deploy och inget behöver köras här.
--
-- Nedan är samma ändringar för hand, om DATABASE_URL inte är satt:

ALTER TABLE public.gw_cache
  ADD COLUMN IF NOT EXISTS points_on_bench INTEGER NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS points_on_hits  INTEGER NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS final           BOOLEAN NOT NULL DEFAULT false,
  ADD COLUMN IF NOT EXISTS season          INTEGER NOT NULL DEFAULT 2025;

-- Säsongen i primärnyckeln gör att förra säsongens GW1 och den nya säsongens
-- GW1 blir olika rader — då behövs ingen manuell rensning inför en ny säsong.
ALTER TABLE public.gw_cache DROP CONSTRAINT IF EXISTS gw_cache_pkey;
ALTER TABLE public.gw_cache ADD PRIMARY KEY (entry_id, season, gw);
ALTER TABLE public.gw_cache ALTER COLUMN season DROP DEFAULT;
