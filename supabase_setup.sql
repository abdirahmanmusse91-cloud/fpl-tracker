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
