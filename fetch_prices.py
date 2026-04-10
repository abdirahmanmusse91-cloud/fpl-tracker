"""
Hämtar historisk spelarprissättning från vaastav/Fantasy-Premier-League
och sparar till Supabase-tabellen player_prices.

Kör: python fetch_prices.py [säsong]
Exempel: python fetch_prices.py 2025-26
"""
import asyncio
import csv
import io
import os
import sys

import httpx

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://gduuhbvkyjcesahmooon.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "sb_publishable_YkC0is9gwKc5xCSflPT8jg_lBfoX5a9")
SB_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "resolution=merge-duplicates",
}

GITHUB_BASE = "https://raw.githubusercontent.com/vaastav/Fantasy-Premier-League/master/data"
SEASON = sys.argv[1] if len(sys.argv) > 1 else "2025-26"


async def fetch_gw(client: httpx.AsyncClient, gw: int) -> list[dict]:
    url = f"{GITHUB_BASE}/{SEASON}/gws/gw{gw}.csv"
    r = await client.get(url)
    if r.status_code == 404:
        return []
    r.raise_for_status()

    rows = []
    seen = set()
    reader = csv.DictReader(io.StringIO(r.text))
    for row in reader:
        element_id = int(row["element"])
        if element_id in seen:
            continue
        seen.add(element_id)
        rows.append({
            "element_id": element_id,
            "season": SEASON,
            "gw": int(row["round"]),
            "name": row["name"],
            "value": int(row["value"]),  # pris × 10, t.ex. 130 = £13.0m
        })
    return rows


async def upsert(client: httpx.AsyncClient, rows: list[dict]) -> None:
    r = await client.post(
        f"{SUPABASE_URL}/rest/v1/player_prices",
        headers=SB_HEADERS,
        json=rows,
    )
    if r.status_code not in (200, 201):
        print(f"  Supabase fel {r.status_code}: {r.text[:200]}")
    else:
        print(f"  Sparade {len(rows)} spelare")


async def cached_gws(client: httpx.AsyncClient) -> set[int]:
    """Hämtar vilka GWs som redan finns i Supabase för denna säsong."""
    r = await client.get(
        f"{SUPABASE_URL}/rest/v1/player_prices",
        headers=SB_HEADERS,
        params={"season": f"eq.{SEASON}", "select": "gw"},
    )
    if r.status_code != 200 or not r.json():
        return set()
    return {row["gw"] for row in r.json()}


async def main():
    print(f"Hämtar priser för säsong {SEASON}...")
    async with httpx.AsyncClient(timeout=20) as client:
        already = await cached_gws(client)
        if already:
            print(f"Redan sparade GWs: {sorted(already)}")

        for gw in range(1, 39):
            if gw in already:
                print(f"GW{gw}: redan sparad, hoppar över")
                continue
            print(f"GW{gw}...", end=" ", flush=True)
            rows = await fetch_gw(client, gw)
            if not rows:
                print("(finns ej hos vaastav, avslutar)")
                break
            await upsert(client, rows)

    print("Klart!")


if __name__ == "__main__":
    asyncio.run(main())
