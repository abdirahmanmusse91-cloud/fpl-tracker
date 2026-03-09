import os
import time
import asyncio
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
import httpx

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

FPL_BASE = "https://fantasy.premierleague.com/api"
FPL_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
}

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://gduuhbvkyjcesahmooon.supabase.co")
SUPABASE_KEY = os.environ.get(
    "SUPABASE_KEY",
    "sb_publishable_YkC0is9gwKc5xCSflPT8jg_lBfoX5a9"
)
SB_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
}

# ── Bootstrap in-memory cache ──
_boot: dict | None = None
_boot_ts: float = 0
BOOT_TTL = 3600  # 1 hour


async def get_bootstrap() -> dict:
    global _boot, _boot_ts
    if _boot and (time.time() - _boot_ts) < BOOT_TTL:
        return _boot
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(f"{FPL_BASE}/bootstrap-static/", headers=FPL_HEADERS)
        r.raise_for_status()
        _boot = r.json()
        _boot_ts = time.time()
    return _boot


def get_finished_gws(boot: dict) -> tuple[list[int], int]:
    """Returns (sorted finished gw ids, current/latest finished gw)."""
    finished = sorted(e["id"] for e in boot["events"] if e["finished"])
    current = finished[-1] if finished else 1
    return finished, current


# ── Supabase helpers (graceful — never raise) ──

async def sb_read(entry_id: int) -> dict[int, int]:
    """Read cached GW→net_points for an entry. Returns {} on any failure."""
    try:
        async with httpx.AsyncClient(timeout=6) as c:
            r = await c.get(
                f"{SUPABASE_URL}/rest/v1/gw_cache",
                headers=SB_HEADERS,
                params={
                    "entry_id": f"eq.{entry_id}",
                    "select": "gw,points",
                    "order": "gw.asc",
                },
            )
            if r.status_code == 200:
                return {row["gw"]: row["points"] for row in r.json()}
    except Exception:
        pass
    return {}


async def sb_write(entry_id: int, data: dict[int, int]) -> None:
    """Upsert net points per GW. Silently ignores failures."""
    if not data:
        return
    rows = [{"entry_id": entry_id, "gw": gw, "points": pts} for gw, pts in data.items()]
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            await c.post(
                f"{SUPABASE_URL}/rest/v1/gw_cache",
                headers={**SB_HEADERS, "Prefer": "resolution=merge-duplicates"},
                json=rows,
            )
    except Exception:
        pass


# ── FPL proxy endpoints ──

@app.get("/api/league/{league_id}")
async def get_league(league_id: int):
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(
            f"{FPL_BASE}/leagues-classic/{league_id}/standings/", headers=FPL_HEADERS
        )
        if r.status_code != 200:
            raise HTTPException(status_code=r.status_code, detail="FPL API error")
        return r.json()


@app.get("/api/bootstrap")
async def bootstrap():
    return await get_bootstrap()


@app.get("/api/entry/{entry_id}/history")
async def get_history(entry_id: int, background_tasks: BackgroundTasks):
    boot = await get_bootstrap()
    all_finished, current_gw = get_finished_gws(boot)

    # Historical = all finished GWs except the current one
    # (current GW may still have pending bonus points)
    historical_set = set(gw for gw in all_finished if gw < current_gw)

    # ── Try Supabase cache ──
    cached = await sb_read(entry_id)
    cached_gws = set(cached.keys())

    if historical_set and historical_set.issubset(cached_gws):
        # Cache hit: fetch only the current (live) GW from FPL
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(
                f"{FPL_BASE}/entry/{entry_id}/history/", headers=FPL_HEADERS
            )
            if r.status_code != 200:
                raise HTTPException(status_code=r.status_code, detail="FPL API error")
            live = r.json()

        # Reconstruct response: cached history + live current GW
        result = [
            {"event": gw, "points": pts, "event_transfers_cost": 0}
            for gw, pts in sorted(cached.items())
            if gw in historical_set
        ]
        live_current = next(
            (e for e in live.get("current", []) if e["event"] == current_gw), None
        )
        if live_current:
            result.append(live_current)

        return {"current": result, "chips": live.get("chips", []), "past": live.get("past", [])}

    # ── Cache miss: fetch all from FPL, update cache in background ──
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(f"{FPL_BASE}/entry/{entry_id}/history/", headers=FPL_HEADERS)
        if r.status_code != 200:
            raise HTTPException(status_code=r.status_code, detail="FPL API error")
        data = r.json()

    to_cache = {
        gw["event"]: gw["points"] - (gw.get("event_transfers_cost") or 0)
        for gw in data.get("current", [])
        if gw["event"] in historical_set
    }
    background_tasks.add_task(sb_write, entry_id, to_cache)

    return data


# ── Dashboard batch endpoint ──

@app.get("/api/dashboard/{league_id}")
async def dashboard(league_id: int, background_tasks: BackgroundTasks):
    """
    Returns everything the frontend needs in one response:
    - managers (league standings from FPL)
    - gw_history: all historical GWs from Supabase, current GW live from FPL
    - current_gw
    This avoids N separate entry/history calls from the frontend.
    """
    boot = await get_bootstrap()
    all_finished, current_gw = get_finished_gws(boot)
    historical_set = set(gw for gw in all_finished if gw < current_gw)

    # League standings from FPL
    async with httpx.AsyncClient(timeout=15) as c:
        league_r = await c.get(
            f"{FPL_BASE}/leagues-classic/{league_id}/standings/", headers=FPL_HEADERS
        )
        if league_r.status_code != 200:
            raise HTTPException(status_code=league_r.status_code, detail="FPL API error")
        managers = league_r.json()["standings"]["results"]

    entry_ids = [m["entry"] for m in managers]
    gw_history: dict[int, dict[int, int]] = {eid: {} for eid in entry_ids}

    # ONE Supabase query for all managers' historical data
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(
                f"{SUPABASE_URL}/rest/v1/gw_cache",
                headers=SB_HEADERS,
                params={
                    "entry_id": f"in.({','.join(str(e) for e in entry_ids)})",
                    "select": "entry_id,gw,points",
                },
            )
        if r.status_code == 200:
            for row in r.json():
                gw_history[row["entry_id"]][row["gw"]] = row["points"]
    except Exception:
        pass

    # Current GW: always fetch live from FPL (parallel for all managers)
    # Also fills in any historical GWs missing from Supabase
    to_cache_all: dict[int, dict[int, int]] = {}

    async def fetch_entry(entry_id: int):
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.get(f"{FPL_BASE}/entry/{entry_id}/history/", headers=FPL_HEADERS)
                data = r.json()
            missing_historical = {}
            for gw in data.get("current", []):
                pts = gw["points"] - (gw.get("event_transfers_cost") or 0)
                if gw["event"] == current_gw:
                    gw_history[entry_id][current_gw] = pts
                elif gw["event"] in historical_set and gw["event"] not in gw_history[entry_id]:
                    gw_history[entry_id][gw["event"]] = pts
                    missing_historical[gw["event"]] = pts
            if missing_historical:
                to_cache_all[entry_id] = missing_historical
        except Exception:
            pass

    await asyncio.gather(*[fetch_entry(eid) for eid in entry_ids])

    # Cache any missing historical data in background
    for eid, data in to_cache_all.items():
        background_tasks.add_task(sb_write, eid, data)

    return {
        "managers": managers,
        "gw_history": gw_history,
        "current_gw": current_gw,
    }


# ── Sync endpoint (called by cron job) ──

LEAGUE_ID = 1076120


@app.post("/api/sync")
async def sync_all():
    """Sync all managers' historical GW data to Supabase."""
    boot = await get_bootstrap()
    all_finished, current_gw = get_finished_gws(boot)
    historical_set = set(gw for gw in all_finished if gw < current_gw)

    if not historical_set:
        return {"message": "No historical GWs to sync", "synced": 0}

    async with httpx.AsyncClient(timeout=15) as c:
        league_r = await c.get(
            f"{FPL_BASE}/leagues-classic/{LEAGUE_ID}/standings/", headers=FPL_HEADERS
        )
        entries = [m["entry"] for m in league_r.json()["standings"]["results"]]

    synced = 0

    async def sync_entry(entry_id: int):
        nonlocal synced
        try:
            cached = await sb_read(entry_id)
            if historical_set.issubset(set(cached.keys())):
                return  # Already up to date
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.get(
                    f"{FPL_BASE}/entry/{entry_id}/history/", headers=FPL_HEADERS
                )
                data = r.json()
            to_cache = {
                gw["event"]: gw["points"] - (gw.get("event_transfers_cost") or 0)
                for gw in data.get("current", [])
                if gw["event"] in historical_set
            }
            await sb_write(entry_id, to_cache)
            synced += 1
        except Exception:
            pass

    await asyncio.gather(*[sync_entry(eid) for eid in entries])
    return {"synced": synced, "total": len(entries), "gws_cached": sorted(historical_set)}


# ── Groq chat (function calling) ──

@app.post("/api/chat")
async def chat(body: dict):
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="GROQ_API_KEY saknas")

    payload = {
        "model": "llama-3.3-70b-versatile",
        "messages": body.get("messages", []),
        "max_tokens": body.get("max_tokens", 1000),
    }
    if "tools" in body:
        payload["tools"] = body["tools"]
        payload["tool_choice"] = "auto"

    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        if r.status_code != 200:
            raise HTTPException(
                status_code=r.status_code, detail=f"Groq API error: {r.text}"
            )
        return {"choice": r.json()["choices"][0]}


@app.get("/")
async def root():
    return FileResponse("index.html")
