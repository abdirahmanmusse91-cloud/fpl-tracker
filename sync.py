"""
Daily sync script — run by Render cron job or manually.
Syncs all managers' historical GW data to Supabase.
"""
import asyncio
import httpx
import os

FPL_BASE = "https://fantasy.premierleague.com/api"
FPL_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
}
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://gduuhbvkyjcesahmooon.supabase.co")
SUPABASE_KEY = os.environ.get(
    "SUPABASE_KEY", "sb_publishable_YkC0is9gwKc5xCSflPT8jg_lBfoX5a9"
)
SB_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
}
LEAGUE_ID = int(os.environ.get("LEAGUE_ID", "1076120"))


async def main():
    async with httpx.AsyncClient(timeout=20) as c:
        boot_r = await c.get(f"{FPL_BASE}/bootstrap-static/", headers=FPL_HEADERS)
        boot = boot_r.json()
        finished = sorted(e["id"] for e in boot["events"] if e["finished"])
        current_gw = finished[-1] if finished else 1
        historical = set(gw for gw in finished if gw < current_gw)

        if not historical:
            print("No historical GWs to sync.")
            return

        league_r = await c.get(
            f"{FPL_BASE}/leagues-classic/{LEAGUE_ID}/standings/", headers=FPL_HEADERS
        )
        managers = league_r.json()["standings"]["results"]
        print(f"Syncing {len(managers)} managers, GWs {min(historical)}–{max(historical)}")

        synced = 0
        for m in managers:
            entry_id = m["entry"]
            try:
                # Check what's already cached
                sb_r = await c.get(
                    f"{SUPABASE_URL}/rest/v1/gw_cache",
                    headers=SB_HEADERS,
                    params={"entry_id": f"eq.{entry_id}", "select": "gw"},
                )
                cached_gws = set(row["gw"] for row in sb_r.json()) if sb_r.status_code == 200 else set()

                missing = historical - cached_gws
                if not missing:
                    print(f"  {m['player_name']}: already up to date")
                    continue

                hist_r = await c.get(
                    f"{FPL_BASE}/entry/{entry_id}/history/", headers=FPL_HEADERS
                )
                data = hist_r.json()
                rows = [
                    {
                        "entry_id": entry_id,
                        "gw": gw["event"],
                        "points": gw["points"] - (gw.get("event_transfers_cost") or 0),
                    }
                    for gw in data.get("current", [])
                    if gw["event"] in missing
                ]
                if rows:
                    await c.post(
                        f"{SUPABASE_URL}/rest/v1/gw_cache",
                        headers={**SB_HEADERS, "Prefer": "resolution=merge-duplicates"},
                        json=rows,
                    )
                    print(f"  {m['player_name']}: cached {len(rows)} GWs")
                    synced += 1
            except Exception as e:
                print(f"  {m['player_name']}: ERROR — {e}")

        print(f"\nDone. Synced {synced}/{len(managers)} managers.")


if __name__ == "__main__":
    asyncio.run(main())
