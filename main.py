import os
import time
import asyncio
from datetime import datetime, timezone, timedelta
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

# How old the current GW entry in Supabase may be before we re-fetch from FPL
CURRENT_GW_TTL = timedelta(minutes=15)

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


# ── Supabase helpers ──

async def sb_read(entry_id: int) -> dict[int, int]:
    """Read cached GW→net_points for a single entry. Returns {} on any failure."""
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


async def sb_read_all(
    entry_ids: list[int],
) -> tuple[dict[int, dict[int, int]], dict[int, dict[int, datetime]]]:
    """
    Batch read all GW data for multiple entries in one Supabase query.
    Returns:
      gw_history[entry_id][gw] = net_points
      synced_at_map[entry_id][gw] = datetime (UTC)
    """
    gw_history: dict[int, dict[int, int]] = {eid: {} for eid in entry_ids}
    synced_at_map: dict[int, dict[int, datetime]] = {eid: {} for eid in entry_ids}
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(
                f"{SUPABASE_URL}/rest/v1/gw_cache",
                headers=SB_HEADERS,
                params={
                    "entry_id": f"in.({','.join(str(e) for e in entry_ids)})",
                    "select": "entry_id,gw,points,synced_at",
                },
            )
        if r.status_code == 200:
            for row in r.json():
                eid = row["entry_id"]
                gw = row["gw"]
                gw_history[eid][gw] = row["points"]
                raw_ts = row.get("synced_at")
                if raw_ts:
                    try:
                        ts = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
                        synced_at_map[eid][gw] = ts
                    except Exception:
                        pass
    except Exception:
        pass
    return gw_history, synced_at_map


async def sb_write(entry_id: int, data: dict[int, int]) -> None:
    """Upsert net points per GW, always writing synced_at so staleness checks work."""
    if not data:
        return
    now = datetime.now(timezone.utc).isoformat()
    rows = [
        {"entry_id": entry_id, "gw": gw, "points": pts, "synced_at": now}
        for gw, pts in data.items()
    ]
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            await c.post(
                f"{SUPABASE_URL}/rest/v1/gw_cache",
                headers={**SB_HEADERS, "Prefer": "resolution=merge-duplicates"},
                json=rows,
            )
    except Exception:
        pass


# ── Dashboard — single endpoint for frontend ──

@app.get("/api/dashboard/{league_id}")
async def dashboard(league_id: int, background_tasks: BackgroundTasks):
    """
    Returns all data the frontend needs in one call.

    Caching strategy:
    - Historical GWs (all finished except current): read from Supabase permanently.
      FPL is only contacted if a GW is missing entirely from Supabase.
    - Current GW: use Supabase if synced_at < CURRENT_GW_TTL (15 min).
      Otherwise re-fetch from FPL and update Supabase.
    - League standings: always from FPL (lightweight, changes each GW).
    """
    boot = await get_bootstrap()
    all_finished, current_gw = get_finished_gws(boot)
    historical_set = set(gw for gw in all_finished if gw < current_gw)

    # League standings — always from FPL (provides manager list + totals)
    async with httpx.AsyncClient(timeout=15) as c:
        league_r = await c.get(
            f"{FPL_BASE}/leagues-classic/{league_id}/standings/", headers=FPL_HEADERS
        )
        if league_r.status_code != 200:
            raise HTTPException(status_code=league_r.status_code, detail="FPL API error")
        managers = league_r.json()["standings"]["results"]

    entry_ids = [m["entry"] for m in managers]

    # One Supabase batch query — all entries, all GWs, including synced_at
    gw_history, synced_at_map = await sb_read_all(entry_ids)

    now = datetime.now(timezone.utc)

    # Decide what needs a live FPL fetch
    needs_current: list[int] = []          # entries where current GW is stale/missing
    needs_historical: dict[int, set[int]] = {}  # entry -> set of missing historical GWs

    for eid in entry_ids:
        # Current GW: stale if synced_at is missing or older than TTL
        current_ts = synced_at_map.get(eid, {}).get(current_gw)
        if current_ts is None or (now - current_ts) > CURRENT_GW_TTL:
            needs_current.append(eid)

        # Historical GWs: permanent — only fetch if genuinely missing from Supabase
        missing = historical_set - set(gw_history.get(eid, {}).keys())
        if missing:
            needs_historical[eid] = missing

    entries_to_fetch = set(needs_current) | set(needs_historical.keys())

    if entries_to_fetch:
        to_cache: dict[int, dict[int, int]] = {}

        async def fetch_entry(entry_id: int):
            try:
                async with httpx.AsyncClient(timeout=15) as c:
                    r = await c.get(
                        f"{FPL_BASE}/entry/{entry_id}/history/", headers=FPL_HEADERS
                    )
                    data = r.json()
                new_data: dict[int, int] = {}
                for gw_row in data.get("current", []):
                    gw_num = gw_row["event"]
                    pts = gw_row["points"] - (gw_row.get("event_transfers_cost") or 0)
                    if gw_num == current_gw and entry_id in needs_current:
                        gw_history[entry_id][current_gw] = pts
                        new_data[current_gw] = pts
                    elif gw_num in needs_historical.get(entry_id, set()):
                        gw_history[entry_id][gw_num] = pts
                        new_data[gw_num] = pts
                if new_data:
                    to_cache[entry_id] = new_data
            except Exception:
                pass

        await asyncio.gather(*[fetch_entry(eid) for eid in entries_to_fetch])

        for eid, data in to_cache.items():
            background_tasks.add_task(sb_write, eid, data)

    return {
        "managers": managers,
        "gw_history": gw_history,
        "current_gw": current_gw,
    }


# ── Legacy per-entry history endpoint (kept for compatibility) ──

@app.get("/api/entry/{entry_id}/history")
async def get_history(entry_id: int, background_tasks: BackgroundTasks):
    boot = await get_bootstrap()
    all_finished, current_gw = get_finished_gws(boot)
    historical_set = set(gw for gw in all_finished if gw < current_gw)

    cached = await sb_read(entry_id)
    cached_gws = set(cached.keys())

    if historical_set and historical_set.issubset(cached_gws):
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(
                f"{FPL_BASE}/entry/{entry_id}/history/", headers=FPL_HEADERS
            )
            if r.status_code != 200:
                raise HTTPException(status_code=r.status_code, detail="FPL API error")
            live = r.json()

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


# ── Bootstrap proxy ──

@app.get("/api/bootstrap")
async def bootstrap():
    return await get_bootstrap()


# ── Sync endpoint (cron job) ──

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
                return
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


# ── Gemini chat ──

@app.post("/api/chat")
async def chat(body: dict):
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return {"error": "config", "message": "AI-tjänsten är inte konfigurerad. Kontakta administratören."}

    messages = body.get("messages", [])
    max_tokens = body.get("max_tokens", 600)

    # Convert OpenAI-style messages to Gemini format
    system_content = None
    contents = []
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content", "")
        if role == "system":
            system_content = content
        elif role == "user":
            contents.append({"role": "user", "parts": [{"text": content}]})
        elif role == "assistant":
            contents.append({"role": "model", "parts": [{"text": content}]})

    payload: dict = {
        "contents": contents,
        "generationConfig": {"maxOutputTokens": max_tokens},
    }
    if system_content:
        payload["systemInstruction"] = {"parts": [{"text": system_content}]}

    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={api_key}",
            headers={"Content-Type": "application/json"},
            json=payload,
        )

    if r.status_code == 429:
        return {"error": "rate_limit", "message": "Systemet vilar – försök igen om en liten stund. ⏳"}
    if r.status_code == 400:
        return {"error": "bad_request", "message": "Jag förstod inte helt. Fråga gärna om poäng, tabell eller spelare! 🤔"}
    if r.status_code != 200:
        return {"error": "api_error", "message": f"Tillfälligt fel ({r.status_code}), försök igen. 🔧"}

    data = r.json()
    candidates = data.get("candidates", [])
    if not candidates:
        finish = data.get("promptFeedback", {}).get("blockReason")
        if finish:
            return {"error": "blocked", "message": "Frågan kunde inte besvaras. Försök omformulera den. 🙏"}
        return {"error": "empty", "message": "Hittade ingen statistik för det du frågade om. 📊"}

    text = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "").strip()
    if not text:
        return {"error": "empty", "message": "Hittade ingen statistik för det du frågade om. 📊"}

    finish_reason = candidates[0].get("finishReason", "")
    if finish_reason == "SAFETY":
        return {"error": "blocked", "message": "Frågan kunde inte besvaras. Försök omformulera den. 🙏"}

    return {"content": text}


@app.get("/api/ping")
async def ping():
    return {"status": "ok"}


@app.get("/")
async def root():
    return FileResponse("index.html")
