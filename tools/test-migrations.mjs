import { PGlite } from '@electric-sql/pglite';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, join } from 'path';

// plocka ut SQL:en ur migrations.py så testet kör exakt samma text som appen
const repoRoot = join(dirname(fileURLToPath(import.meta.url)), '..');
const py = readFileSync(join(repoRoot, 'migrations.py'), 'utf8');
const tracking = py.match(/TRACKING_TABLE = """([\s\S]*?)"""/)[1];
const migs = [...py.matchAll(/\("(\d{3}_\w+)", """([\s\S]*?)"""\)/g)].map(m => [m[1], m[2]]);
console.log('hittade migrationer:', migs.map(m => m[0]).join(', '));

const db = new PGlite();

// återskapa tabellen som den såg ut FÖRE alla migrationer (mars-tillståndet)
await db.exec(`
  CREATE TABLE public.gw_cache (
    entry_id  INTEGER NOT NULL,
    gw        INTEGER NOT NULL,
    points    INTEGER NOT NULL,
    synced_at TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (entry_id, gw)
  );
  INSERT INTO public.gw_cache (entry_id, gw, points, synced_at)
  VALUES (24071, 1, 50, '2026-03-24'), (24071, 2, 61, '2026-03-24'), (6318928, 1, 55, '2026-03-24');
`);

async function runAll(label) {
  await db.exec(tracking);
  const done = new Set((await db.query('SELECT name FROM public.schema_migrations')).rows.map(r => r.name));
  const applied = [];
  for (const [name, sql] of migs) {
    if (done.has(name)) continue;
    await db.exec('BEGIN');
    try {
      await db.exec(sql);
      await db.query('INSERT INTO public.schema_migrations (name) VALUES ($1)', [name]);
      await db.exec('COMMIT');
      applied.push(name);
    } catch (e) { await db.exec('ROLLBACK'); throw new Error(`${name}: ${e.message}`); }
  }
  console.log(`${label}: körde [${applied.join(', ') || 'inga'}]`);
}

await runAll('första körningen');
await runAll('andra körningen (ska vara no-op)');

const cols = await db.query(`SELECT column_name, data_type, is_nullable FROM information_schema.columns
                             WHERE table_name='gw_cache' ORDER BY ordinal_position`);
console.log('\nkolumner:', cols.rows.map(r => `${r.column_name}:${r.data_type}`).join(', '));

const pk = await db.query(`SELECT a.attname FROM pg_index i
  JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
  WHERE i.indrelid = 'public.gw_cache'::regclass AND i.indisprimary`);
console.log('primärnyckel:', pk.rows.map(r => r.attname).join(' + '));

const old = await db.query('SELECT entry_id, season, gw, points FROM public.gw_cache ORDER BY entry_id, gw');
console.log('gamla rader efter migrering:', JSON.stringify(old.rows));

// kan nya säsongens GW1 samexistera med förra säsongens?
await db.query(`INSERT INTO public.gw_cache (entry_id, season, gw, points, points_on_bench, points_on_hits, final)
                VALUES (24071, 2026, 1, 77, 3, 0, false)`);
const both = await db.query('SELECT season, gw, points FROM public.gw_cache WHERE entry_id=24071 AND gw=1 ORDER BY season');
console.log('entry 24071, GW1 per säsong:', JSON.stringify(both.rows));

// upsert på nya nyckeln
await db.query(`INSERT INTO public.gw_cache (entry_id, season, gw, points, points_on_bench, points_on_hits, final)
                VALUES (24071, 2026, 1, 84, 3, 0, true)
                ON CONFLICT (entry_id, season, gw) DO UPDATE SET points=EXCLUDED.points, final=EXCLUDED.final`);
const up = await db.query('SELECT points, final FROM public.gw_cache WHERE entry_id=24071 AND season=2026 AND gw=1');
console.log('efter upsert:', JSON.stringify(up.rows));
await db.close();
