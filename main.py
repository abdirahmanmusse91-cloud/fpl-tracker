"""FPL Liga Tracker — FastAPI backend"""
import os
import time
import math
import json
import asyncio
from pathlib import Path
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
import httpx

BASE_DIR = Path(__file__).parent

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
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "sb_publishable_YkC0is9gwKc5xCSflPT8jg_lBfoX5a9")
SB_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
}

CURRENT_GW_TTL = timedelta(minutes=15)
QUARTERS = {1: (1, 10), 2: (11, 20), 3: (21, 30), 4: (31, 38)}
LEAGUE_ID = 1076120

# ── Bootstrap in-memory cache ──
_boot: dict | None = None
_boot_ts: float = 0
BOOT_TTL = 3600


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
    events = boot.get("events", [])
    finished = sorted(e["id"] for e in events if e["finished"])
    # Use the is_current event as current_gw (may be live/unfinished)
    current = next((e["id"] for e in events if e.get("is_current")), None)
    if current is None:
        current = finished[-1] if finished else 1
    return finished, current


def is_gw_live(boot: dict, current_gw: int) -> bool:
    active = next((e for e in boot.get("events", []) if e["id"] == current_gw), None)
    return active is not None and not active.get("finished", True)


# ── Supabase: logs ──

async def sb_log(level: str, message: str, endpoint: str = None,
                 status_code: int = None, duration_ms: int = None, metadata: dict = None) -> None:
    try:
        async with httpx.AsyncClient(timeout=4) as c:
            await c.post(
                f"{SUPABASE_URL}/rest/v1/logs",
                headers=SB_HEADERS,
                json={
                    "level": level,
                    "endpoint": endpoint,
                    "message": message,
                    "status_code": status_code,
                    "duration_ms": duration_ms,
                    "metadata": metadata,
                },
            )
    except Exception:
        pass


# ── Supabase: gw_cache ──

async def sb_read(entry_id: int) -> dict[int, int]:
    try:
        async with httpx.AsyncClient(timeout=6) as c:
            r = await c.get(
                f"{SUPABASE_URL}/rest/v1/gw_cache",
                headers=SB_HEADERS,
                params={"entry_id": f"eq.{entry_id}", "select": "gw,points", "order": "gw.asc"},
            )
            if r.status_code == 200:
                return {row["gw"]: row["points"] for row in r.json()}
    except Exception:
        pass
    return {}


async def sb_read_all(entry_ids: list[int]) -> tuple[dict, dict, dict]:
    gw_history: dict[int, dict[int, int]] = {eid: {} for eid in entry_ids}
    bench_history: dict[int, dict[int, int]] = {eid: {} for eid in entry_ids}
    synced_at_map: dict[int, dict[int, datetime]] = {eid: {} for eid in entry_ids}
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(
                f"{SUPABASE_URL}/rest/v1/gw_cache",
                headers=SB_HEADERS,
                params={
                    "entry_id": f"in.({','.join(str(e) for e in entry_ids)})",
                    "select": "entry_id,gw,points,points_on_bench,synced_at",
                },
            )
        if r.status_code == 200:
            for row in r.json():
                eid = row["entry_id"]
                gw_history[eid][row["gw"]] = row["points"]
                bench_history[eid][row["gw"]] = row.get("points_on_bench") or 0
                raw_ts = row.get("synced_at")
                if raw_ts:
                    try:
                        synced_at_map[eid][row["gw"]] = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
                    except Exception:
                        pass
    except Exception:
        pass
    return gw_history, synced_at_map, bench_history


async def sb_write(entry_id: int, data: dict[int, int], bench_data: dict[int, int] = None) -> None:
    if not data:
        return
    now = datetime.now(timezone.utc).isoformat()
    rows = [{"entry_id": entry_id, "gw": gw, "points": pts,
             "points_on_bench": (bench_data or {}).get(gw, 0), "synced_at": now}
            for gw, pts in data.items()]
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            await c.post(
                f"{SUPABASE_URL}/rest/v1/gw_cache",
                headers={**SB_HEADERS, "Prefer": "resolution=merge-duplicates"},
                json=rows,
            )
    except Exception:
        pass


# ── Supabase: managers_cache ──

async def sb_upsert_managers(managers: list) -> None:
    now = datetime.now(timezone.utc).isoformat()
    rows = [
        {"entry_id": m["entry"], "player_name": m["player_name"],
         "entry_name": m["entry_name"], "total": m["total"], "rank": i + 1, "updated_at": now}
        for i, m in enumerate(managers)
    ]
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            await c.post(
                f"{SUPABASE_URL}/rest/v1/managers_cache",
                headers={**SB_HEADERS, "Prefer": "resolution=merge-duplicates"},
                json=rows,
            )
    except Exception:
        pass


async def sb_read_managers() -> list:
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.get(
                f"{SUPABASE_URL}/rest/v1/managers_cache",
                headers=SB_HEADERS,
                params={"select": "*", "order": "rank.asc"},
            )
            if r.status_code == 200:
                return [
                    {"entry": row["entry_id"], "player_name": row["player_name"],
                     "entry_name": row["entry_name"], "total": row["total"]}
                    for row in r.json()
                ]
    except Exception:
        pass
    return []


# ── Supabase: league_state ──

async def sb_update_league_state(league_id: int, current_gw: int, live: bool, managers: list) -> None:
    if not managers:
        return
    leader = managers[0]
    row = {
        "league_id": league_id,
        "current_gw": current_gw,
        "is_live": live,
        "leader_entry_id": leader["entry"],
        "leader_name": leader["player_name"],
        "leader_total": leader["total"],
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            await c.post(
                f"{SUPABASE_URL}/rest/v1/league_state",
                headers={**SB_HEADERS, "Prefer": "resolution=merge-duplicates"},
                json=row,
            )
    except Exception:
        pass


async def sb_get_league_state(league_id: int) -> dict | None:
    try:
        async with httpx.AsyncClient(timeout=6) as c:
            r = await c.get(
                f"{SUPABASE_URL}/rest/v1/league_state",
                headers=SB_HEADERS,
                params={"league_id": f"eq.{league_id}", "select": "*"},
            )
            if r.status_code == 200:
                rows = r.json()
                return rows[0] if rows else None
    except Exception:
        pass
    return None


# ── Supabase: chat_history ──

async def sb_save_chat(session_id: str, role: str, content: str) -> None:
    try:
        async with httpx.AsyncClient(timeout=6) as c:
            await c.post(
                f"{SUPABASE_URL}/rest/v1/chat_history",
                headers=SB_HEADERS,
                json={"session_id": session_id, "role": role, "content": content},
            )
    except Exception:
        pass


async def sb_load_chat(session_id: str, limit: int = 20) -> list:
    try:
        async with httpx.AsyncClient(timeout=6) as c:
            r = await c.get(
                f"{SUPABASE_URL}/rest/v1/chat_history",
                headers=SB_HEADERS,
                params={
                    "session_id": f"eq.{session_id}",
                    "select": "role,content",
                    "order": "created_at.asc",
                    "limit": limit,
                },
            )
            if r.status_code == 200:
                return [{"role": row["role"], "content": row["content"]} for row in r.json()]
    except Exception:
        pass
    return []


# ── Supabase: notification_subscribers ──

async def sb_get_subscribers() -> list:
    try:
        async with httpx.AsyncClient(timeout=6) as c:
            r = await c.get(
                f"{SUPABASE_URL}/rest/v1/notification_subscribers",
                headers=SB_HEADERS,
                params={"active": "eq.true", "select": "email,name"},
            )
            if r.status_code == 200:
                return r.json()
    except Exception:
        pass
    return []


# ── Dashboard ──

@app.get("/api/dashboard/{league_id}")
async def dashboard(league_id: int, background_tasks: BackgroundTasks):
    boot = await get_bootstrap()
    all_finished, current_gw = get_finished_gws(boot)
    live = is_gw_live(boot, current_gw)
    # historical = all finished GWs; if current GW is live, include it too for live scores
    historical_set = set(gw for gw in all_finished if gw < current_gw)
    gws_to_fetch = historical_set | {current_gw}

    async with httpx.AsyncClient(timeout=15) as c:
        league_r = await c.get(
            f"{FPL_BASE}/leagues-classic/{league_id}/standings/", headers=FPL_HEADERS
        )
        if league_r.status_code != 200:
            raise HTTPException(status_code=league_r.status_code, detail="FPL API error")
        managers = league_r.json()["standings"]["results"]

    entry_ids = [m["entry"] for m in managers]
    gw_history, synced_at_map, bench_history = await sb_read_all(entry_ids)

    now = datetime.now(timezone.utc)
    needs_current: list[int] = []
    needs_historical: dict[int, set[int]] = {}

    for eid in entry_ids:
        current_ts = synced_at_map.get(eid, {}).get(current_gw)
        if current_ts is None or (now - current_ts) > CURRENT_GW_TTL:
            needs_current.append(eid)
        missing = gws_to_fetch - set(gw_history.get(eid, {}).keys())
        if missing:
            needs_historical[eid] = missing

    entries_to_fetch = set(needs_current) | set(needs_historical.keys())

    if entries_to_fetch:
        to_cache: dict[int, dict[int, int]] = {}
        bench_to_cache: dict[int, dict[int, int]] = {}

        async def fetch_entry(entry_id: int):
            try:
                async with httpx.AsyncClient(timeout=15) as c:
                    r = await c.get(f"{FPL_BASE}/entry/{entry_id}/history/", headers=FPL_HEADERS)
                    data = r.json()
                new_data: dict[int, int] = {}
                new_bench: dict[int, int] = {}
                for gw_row in data.get("current", []):
                    gw_num = gw_row["event"]
                    pts = gw_row["points"] - (gw_row.get("event_transfers_cost") or 0)
                    bench = gw_row.get("points_on_bench") or 0
                    if gw_num == current_gw and entry_id in needs_current:
                        gw_history[entry_id][current_gw] = pts
                        bench_history[entry_id][current_gw] = bench
                        new_data[current_gw] = pts
                        new_bench[current_gw] = bench
                    elif gw_num in needs_historical.get(entry_id, set()):
                        gw_history[entry_id][gw_num] = pts
                        bench_history[entry_id][gw_num] = bench
                        new_data[gw_num] = pts
                        new_bench[gw_num] = bench
                if new_data:
                    to_cache[entry_id] = new_data
                    bench_to_cache[entry_id] = new_bench
            except Exception:
                pass

        await asyncio.gather(*[fetch_entry(eid) for eid in entries_to_fetch])
        for eid, data in to_cache.items():
            background_tasks.add_task(sb_write, eid, data, bench_to_cache.get(eid))

    background_tasks.add_task(sb_upsert_managers, managers)
    background_tasks.add_task(sb_update_league_state, league_id, current_gw, live, managers)

    return {
        "managers": managers,
        "gw_history": gw_history,
        "bench_history": bench_history,
        "current_gw": current_gw,
        "is_live": live,
    }


# ── Legacy per-entry history endpoint ──

@app.get("/api/entry/{entry_id}/history")
async def get_history(entry_id: int, background_tasks: BackgroundTasks):
    boot = await get_bootstrap()
    all_finished, current_gw = get_finished_gws(boot)
    historical_set = set(gw for gw in all_finished if gw < current_gw)

    cached = await sb_read(entry_id)
    cached_gws = set(cached.keys())

    if historical_set and historical_set.issubset(cached_gws):
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(f"{FPL_BASE}/entry/{entry_id}/history/", headers=FPL_HEADERS)
            if r.status_code != 200:
                raise HTTPException(status_code=r.status_code, detail="FPL API error")
            live_data = r.json()
        result = [
            {"event": gw, "points": pts, "event_transfers_cost": 0}
            for gw, pts in sorted(cached.items()) if gw in historical_set
        ]
        live_current = next((e for e in live_data.get("current", []) if e["event"] == current_gw), None)
        if live_current:
            result.append(live_current)
        return {"current": result, "chips": live_data.get("chips", []), "past": live_data.get("past", [])}

    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(f"{FPL_BASE}/entry/{entry_id}/history/", headers=FPL_HEADERS)
        if r.status_code != 200:
            raise HTTPException(status_code=r.status_code, detail="FPL API error")
        data = r.json()

    to_cache = {
        gw["event"]: gw["points"] - (gw.get("event_transfers_cost") or 0)
        for gw in data.get("current", []) if gw["event"] in historical_set
    }
    background_tasks.add_task(sb_write, entry_id, to_cache)
    return data


# ── Bootstrap proxy ──

@app.get("/api/bootstrap")
async def bootstrap_proxy():
    return await get_bootstrap()


# ── Sync endpoint ──

@app.post("/api/sync")
async def sync_all():
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
                r = await c.get(f"{FPL_BASE}/entry/{entry_id}/history/", headers=FPL_HEADERS)
                data = r.json()
            to_cache = {
                gw["event"]: gw["points"] - (gw.get("event_transfers_cost") or 0)
                for gw in data.get("current", []) if gw["event"] in historical_set
            }
            await sb_write(entry_id, to_cache)
            synced += 1
        except Exception:
            pass

    await asyncio.gather(*[sync_entry(eid) for eid in entries])
    return {"synced": synced, "total": len(entries), "gws_cached": sorted(historical_set)}


# ── Chat: Groq/Llama function calling tools ──

def _tool(name, desc, props=None, required=None):
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props or {},
                       **({"required": required} if required else {})}}}

_gw   = {"gw": {"type": "integer"}}
_fqt  = {"from_gw": {"type": "integer"}, "to_gw": {"type": "integer"}, "quarter": {"type": "integer"}}
_m12  = {"manager1": {"type": "string"}, "manager2": {"type": "string"}}

GROQ_TOOLS = [
    _tool("get_standings",          "Ligatabell: ranking och totalpoäng"),
    _tool("get_gw_scores",          "Poäng per spelare i en omgång",              _gw, ["gw"]),
    _tool("get_range_scores",       "Summa poäng GW-intervall",                   {"from_gw": {"type": "integer"}, "to_gw": {"type": "integer"}}, ["from_gw", "to_gw"]),
    _tool("get_quarter_scores",     "Poäng kvartal 1-4",                          {"quarter": {"type": "integer"}}, ["quarter"]),
    _tool("get_consistency",        "Snitt, stddev, min, max per spelare"),
    _tool("get_gw_wins",            "Antal omgångsvinster per spelare",            {"manager_name": {"type": "string"}}),
    _tool("get_standings_at_gw",    "Ackumulerad tabell vid given omgång",         _gw, ["gw"]),
    _tool("get_head_to_head",       "Head-to-head mellan två spelare",            _m12, ["manager1", "manager2"]),
    _tool("get_all_time_best_gw",   "Bästa enskilda omgångsprestationer",         {"top_n": {"type": "integer"}}),
    _tool("get_overtake_gw",        "Omgångar då spelare gick om varandra",       _m12, ["manager1", "manager2"]),
    _tool("get_quarter_progression","Kvartalstabell GW för GW",                   {"quarter": {"type": "integer"}}, ["quarter"]),
    _tool("get_streaks",            "Nuvarande vinst/förlust-streak per spelare"),
    _tool("get_all_time_worst_gw",  "Sämsta enskilda omgångsprestationer",        {"bottom_n": {"type": "integer"}}),
    _tool("get_bench_points",       "Bänkpoäng per spelare, filter: kvartal/GW",  _fqt),
    _tool("get_standings_with_bench","Tabell med bänkpoäng inräknade",            _fqt),
]


def make_tool_fns(managers: list, gw_history: dict, current_gw: int, bench_history: dict = None) -> dict:
    bench_history = bench_history or {}
    def _entry(name):
        return next((m for m in managers if name.lower() in m["player_name"].lower()), None)

    def get_standings():
        return [{"rank": i + 1, "name": m["player_name"], "total": m["total"]}
                for i, m in enumerate(managers)]

    def get_gw_scores(gw):
        return sorted(
            [{"name": m["player_name"], "pts": gw_history.get(m["entry"], {}).get(gw, 0)}
             for m in managers], key=lambda x: -x["pts"])

    def get_range_scores(from_gw, to_gw):
        r = range(from_gw, min(to_gw, current_gw) + 1)
        return sorted(
            [{"name": m["player_name"], "pts": sum(gw_history.get(m["entry"], {}).get(g, 0) for g in r)}
             for m in managers], key=lambda x: -x["pts"])

    def get_quarter_scores(quarter):
        f, t = QUARTERS.get(quarter, (1, current_gw))
        return get_range_scores(f, t)

    def get_consistency():
        result = []
        for m in managers:
            scores = [gw_history.get(m["entry"], {}).get(g, 0) for g in range(1, current_gw + 1)]
            if not scores:
                continue
            avg = sum(scores) / len(scores)
            stddev = math.sqrt(sum((p - avg) ** 2 for p in scores) / len(scores))
            result.append({"name": m["player_name"], "avg": round(avg, 1), "stddev": round(stddev, 1),
                           "min": min(scores), "max": max(scores)})
        return sorted(result, key=lambda x: x["stddev"])

    def get_gw_wins(manager_name=None):
        wins = {m["player_name"]: 0 for m in managers}
        for gw in range(1, current_gw + 1):
            scores = [gw_history.get(m["entry"], {}).get(gw, 0) for m in managers]
            mx = max(scores) if scores else 0
            if mx > 0:
                for m, s in zip(managers, scores):
                    if s == mx:
                        wins[m["player_name"]] += 1
        result = sorted([{"name": n, "wins": w} for n, w in wins.items()], key=lambda x: -x["wins"])
        if manager_name:
            found = next((r for r in result if manager_name.lower() in r["name"].lower()), None)
            return found or {"error": f"Hittade inte {manager_name}"}
        return result

    def get_standings_at_gw(gw):
        cap = min(gw, current_gw)
        result = [{"name": m["player_name"],
                   "pts": sum(gw_history.get(m["entry"], {}).get(g, 0) for g in range(1, cap + 1))}
                  for m in managers]
        return [{"rank": i + 1, **m} for i, m in enumerate(sorted(result, key=lambda x: -x["pts"]))]

    def get_head_to_head(manager1, manager2):
        m1, m2 = _entry(manager1), _entry(manager2)
        if not m1 or not m2:
            return {"error": f"Hittade inte {manager1} eller {manager2}"}
        w1 = w2 = draws = 0
        for gw in range(1, current_gw + 1):
            p1 = gw_history.get(m1["entry"], {}).get(gw, 0)
            p2 = gw_history.get(m2["entry"], {}).get(gw, 0)
            if p1 > p2:
                w1 += 1
            elif p2 > p1:
                w2 += 1
            else:
                draws += 1
        return {"manager1": {"name": m1["player_name"], "wins": w1},
                "manager2": {"name": m2["player_name"], "wins": w2},
                "draws": draws, "total_gws": current_gw}

    def get_all_time_best_gw(top_n=None):
        n = top_n or 5
        records = [{"name": m["player_name"], "gw": gw, "pts": gw_history.get(m["entry"], {}).get(gw, 0)}
                   for gw in range(1, current_gw + 1) for m in managers
                   if gw_history.get(m["entry"], {}).get(gw, 0) > 0]
        return sorted(records, key=lambda x: -x["pts"])[:n]

    def get_overtake_gw(manager1, manager2):
        m1, m2 = _entry(manager1), _entry(manager2)
        if not m1 or not m2:
            return {"error": f"Hittade inte {manager1} eller {manager2}"}
        cum1 = cum2 = 0
        overtakes = []
        for gw in range(1, current_gw + 1):
            prev1, prev2 = cum1, cum2
            cum1 += gw_history.get(m1["entry"], {}).get(gw, 0)
            cum2 += gw_history.get(m2["entry"], {}).get(gw, 0)
            if prev1 <= prev2 and cum1 > cum2:
                overtakes.append({"gw": gw, "event": f"{m1['player_name']} gick om {m2['player_name']}", "gap": cum1 - cum2})
            if prev2 <= prev1 and cum2 > cum1:
                overtakes.append({"gw": gw, "event": f"{m2['player_name']} gick om {m1['player_name']}", "gap": cum2 - cum1})
        return overtakes

    def get_streaks():
        result = []
        for m in managers:
            streak = 0
            streak_type = None
            for gw in range(current_gw, 0, -1):
                scores = sorted(
                    [(m2["player_name"], gw_history.get(m2["entry"], {}).get(gw, 0)) for m2 in managers],
                    key=lambda x: -x[1]
                )
                my_pts = gw_history.get(m["entry"], {}).get(gw, 0)
                if my_pts == 0:
                    break
                if my_pts == scores[0][1]:
                    gw_result = "win"
                elif my_pts == scores[-1][1]:
                    gw_result = "loss"
                else:
                    gw_result = "mid"
                if streak == 0:
                    streak_type = gw_result
                    streak = 1
                elif gw_result == streak_type:
                    streak += 1
                else:
                    break
            label = "vinster" if streak_type == "win" else "förluster" if streak_type == "loss" else "mellanplaceringar"
            result.append({"name": m["player_name"], "streak": streak, "type": streak_type, "desc": f"{streak} {label} i rad"})
        return sorted(result, key=lambda x: -x["streak"])

    def get_all_time_worst_gw(bottom_n=None):
        n = bottom_n or 5
        records = [{"name": m["player_name"], "gw": gw, "pts": gw_history.get(m["entry"], {}).get(gw, 0)}
                   for gw in range(1, current_gw + 1) for m in managers
                   if gw_history.get(m["entry"], {}).get(gw, 0) > 0]
        return sorted(records, key=lambda x: x["pts"])[:n]

    def get_bench_points(from_gw=None, to_gw=None, quarter=None):
        if quarter:
            f, t = QUARTERS.get(quarter, (1, current_gw))
        else:
            f, t = from_gw or 1, to_gw or current_gw
        r = range(f, min(t, current_gw) + 1)
        result = [
            {"name": m["player_name"],
             "bench_pts": sum(bench_history.get(m["entry"], {}).get(g, 0) for g in r),
             "actual_pts": sum(gw_history.get(m["entry"], {}).get(g, 0) for g in r)}
            for m in managers
        ]
        return sorted(result, key=lambda x: -x["bench_pts"])

    def get_standings_with_bench(from_gw=None, to_gw=None, quarter=None):
        if quarter:
            f, t = QUARTERS.get(quarter, (1, current_gw))
        else:
            f, t = from_gw or 1, to_gw or current_gw
        r = range(f, min(t, current_gw) + 1)
        result = []
        for m in managers:
            actual = sum(gw_history.get(m["entry"], {}).get(g, 0) for g in r)
            bench = sum(bench_history.get(m["entry"], {}).get(g, 0) for g in r)
            result.append({"name": m["player_name"], "actual_pts": actual,
                           "bench_pts": bench, "combined_pts": actual + bench})
        return [{"rank": i + 1, **m} for i, m in
                enumerate(sorted(result, key=lambda x: -x["combined_pts"]))]

    def get_quarter_progression(quarter):
        f, t = QUARTERS.get(quarter, (1, current_gw))
        gws = range(f, min(t, current_gw) + 1)
        # Cumulative points within the quarter per GW
        result = []
        for gw in gws:
            cumulative = {}
            for m in managers:
                cumulative[m["player_name"]] = sum(
                    gw_history.get(m["entry"], {}).get(g, 0) for g in range(f, gw + 1)
                )
            ranked = sorted(cumulative.items(), key=lambda x: -x[1])
            leader = ranked[0][0]
            result.append({
                "gw": gw,
                "leader": leader,
                "standings": [{"rank": i+1, "name": n, "pts": p} for i, (n, p) in enumerate(ranked)]
            })
        return result

    return {
        "get_standings": get_standings, "get_gw_scores": get_gw_scores,
        "get_range_scores": get_range_scores, "get_quarter_scores": get_quarter_scores,
        "get_consistency": get_consistency, "get_gw_wins": get_gw_wins,
        "get_standings_at_gw": get_standings_at_gw, "get_head_to_head": get_head_to_head,
        "get_all_time_best_gw": get_all_time_best_gw, "get_overtake_gw": get_overtake_gw,
        "get_quarter_progression": get_quarter_progression,
        "get_streaks": get_streaks, "get_all_time_worst_gw": get_all_time_worst_gw,
        "get_bench_points": get_bench_points, "get_standings_with_bench": get_standings_with_bench,
    }


def execute_tool(name: str, args: dict, tool_fns: dict):
    try:
        if name == "get_standings":         return tool_fns["get_standings"]()
        if name == "get_gw_scores":         return tool_fns["get_gw_scores"](args["gw"])
        if name == "get_range_scores":      return tool_fns["get_range_scores"](args["from_gw"], args["to_gw"])
        if name == "get_quarter_scores":    return tool_fns["get_quarter_scores"](args["quarter"])
        if name == "get_consistency":       return tool_fns["get_consistency"]()
        if name == "get_gw_wins":           return tool_fns["get_gw_wins"](args.get("manager_name"))
        if name == "get_standings_at_gw":   return tool_fns["get_standings_at_gw"](args["gw"])
        if name == "get_head_to_head":      return tool_fns["get_head_to_head"](args["manager1"], args["manager2"])
        if name == "get_all_time_best_gw":  return tool_fns["get_all_time_best_gw"](args.get("top_n"))
        if name == "get_overtake_gw":          return tool_fns["get_overtake_gw"](args["manager1"], args["manager2"])
        if name == "get_quarter_progression":  return tool_fns["get_quarter_progression"](args["quarter"])
        if name == "get_streaks":               return tool_fns["get_streaks"]()
        if name == "get_all_time_worst_gw":     return tool_fns["get_all_time_worst_gw"](args.get("bottom_n"))
        if name == "get_bench_points":          return tool_fns["get_bench_points"](args.get("from_gw"), args.get("to_gw"), args.get("quarter"))
        if name == "get_standings_with_bench":  return tool_fns["get_standings_with_bench"](args.get("from_gw"), args.get("to_gw"), args.get("quarter"))
        return {"error": f"Unknown tool: {name}"}
    except Exception as e:
        return {"error": str(e)}


# ── Chat endpoint ──

AI_ERROR_MESSAGES = {
    "rate_limit":  "För många frågor på kort tid – vänta en minut och försök igen. ⏳",
    "daily_limit": "Dagens frågegräns är nådd – chatten öppnar igen vid midnatt. 🌙",
    "empty":       "Kunde inte svara på det – försök omformulera frågan. 🤔",
    "bad_request": "Jag förstod inte helt. Fråga gärna om poäng, tabell eller spelare! 🤔",
    "blocked":     "Den frågan kan jag inte svara på – håll dig till liga och statistik. ⚽",
    "config":      "AI-tjänsten är inte konfigurerad. Kontakta administratören. ⚙️",
    "api_error":   "Tjänsten är tillfälligt otillgänglig – försök igen om en stund. 🔧",
}


@app.post("/api/chat")
async def chat(body: dict, background_tasks: BackgroundTasks):
    t_start = time.time()
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        return {"error": "config", "message": AI_ERROR_MESSAGES["config"]}

    user_message = body.get("user_message", "").strip()
    history = body.get("history", [])
    session_id = body.get("session_id", "default")
    league_id = body.get("league_id", LEAGUE_ID)

    if not user_message:
        return {"error": "bad_request", "message": AI_ERROR_MESSAGES["bad_request"]}

    # Fetch managers from cache, fallback to FPL
    managers = await sb_read_managers()
    if not managers:
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.get(f"{FPL_BASE}/leagues-classic/{league_id}/standings/", headers=FPL_HEADERS)
                managers = r.json()["standings"]["results"] if r.status_code == 200 else []
        except Exception:
            return {"error": "api_error", "message": "Kunde inte hämta ligadata. Försök igen. 🔧"}

    entry_ids = [m["entry"] for m in managers]
    gw_history, _, bench_history = await sb_read_all(entry_ids)

    state = await sb_get_league_state(league_id)
    current_gw = state["current_gw"] if state else 1

    tool_fns = make_tool_fns(managers, gw_history, current_gw, bench_history)
    player_names = ", ".join(m["player_name"] for m in managers)

    system_prompt = (
        f"FPL-assistent, liga 'Big Blinds' ({len(managers)} spelare, GW{current_gw}). "
        f"K1=GW1-10,K2=GW11-20,K3=GW21-30,K4=GW31-38. Spelare: {player_names}. "
        f"Svara på svenska med emojis. Kortfattad men fullständig. "
        f"Använd ALLTID verktyg för data—uppfinn aldrig siffror. Nämn alla spelare som uppfyller kriteriet. "
        f"Räkna inte differenser själv—hämta via verktyg. "
        f"Konsistens: nämn alltid stddev+snitt. Låg stddev+lågt snitt=konsekvent dålig; låg+högt=bra; hög+högt=toppar/dalar."
    )

    messages = [{"role": "system", "content": system_prompt}]
    for msg in history[-6:]:
        role = msg.get("role")
        if role in ("user", "assistant"):
            messages.append({"role": role, "content": msg.get("content", "")})
    messages.append({"role": "user", "content": user_message})

    answer = None
    for _ in range(6):
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={"model": "llama-3.3-70b-versatile", "messages": messages,
                      "tools": GROQ_TOOLS, "max_tokens": 800},
            )

        if r.status_code != 200:
            print(f"[Groq] HTTP {r.status_code}: {r.text[:500]}")
            ms = int((time.time() - t_start) * 1000)
            if r.status_code == 429:
                is_daily = "per day" in r.text.lower() or "tpd" in r.text.lower() or "rpd" in r.text.lower()
                err_key = "daily_limit" if is_daily else "rate_limit"
                background_tasks.add_task(sb_log, "warn", f"Groq {err_key}", "/api/chat", 429, ms,
                                          {"question": user_message[:100], "error": r.text[:200]})
                return {"error": err_key, "message": AI_ERROR_MESSAGES[err_key]}
            if r.status_code == 400 and "tool_use_failed" in r.text:
                # Llama generated malformed tool JSON — retry with only current message
                messages = [{"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_message}]
                continue
            background_tasks.add_task(sb_log, "error", f"Groq HTTP {r.status_code}", "/api/chat", r.status_code, ms,
                                      {"question": user_message[:100], "error": r.text[:200]})
            return {"error": "api_error", "message": AI_ERROR_MESSAGES["api_error"]}

        data = r.json()
        choice = data["choices"][0]
        msg_out = choice["message"]
        finish_reason = choice.get("finish_reason", "")

        tool_calls = msg_out.get("tool_calls") or []
        if tool_calls:
            messages.append(msg_out)
            for tc in tool_calls:
                fn_name = tc["function"]["name"]
                fn_args = json.loads(tc["function"].get("arguments") or "{}")
                result = execute_tool(fn_name, fn_args, tool_fns)
                messages.append({"role": "tool", "tool_call_id": tc["id"], "content": json.dumps(result)})
        else:
            text = (msg_out.get("content") or "").strip()
            if text:
                answer = text
            break

    ms = int((time.time() - t_start) * 1000)
    if not answer:
        background_tasks.add_task(sb_log, "warn", "Groq empty response", "/api/chat", 200, ms,
                                  {"question": user_message[:100]})
        return {"error": "empty", "message": AI_ERROR_MESSAGES["empty"]}

    background_tasks.add_task(sb_log, "info", "Chat OK", "/api/chat", 200, ms,
                              {"question": user_message[:100]})

    return {"content": answer}


# ── Chat history ──

@app.get("/api/chat/history/{session_id}")
async def chat_history(session_id: str):
    messages = await sb_load_chat(session_id)
    return {"messages": messages}


# ── Notification subscribe ──

@app.post("/api/notifications/subscribe")
async def subscribe(body: dict):
    email = body.get("email", "").strip()
    name = body.get("name", "").strip()
    if not email or "@" not in email:
        return {"error": "Ogiltig e-postadress"}
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.post(
                f"{SUPABASE_URL}/rest/v1/notification_subscribers",
                headers={**SB_HEADERS, "Prefer": "resolution=merge-duplicates"},
                json={"email": email, "name": name or None},
            )
            if r.status_code in (200, 201):
                return {"ok": True, "message": "Du prenumererar nu på notifieringar! 🎉"}
        return {"error": "Kunde inte spara, försök igen."}
    except Exception:
        return {"error": "Serverfel, försök igen."}


# ── Logs ──

@app.get("/api/logs")
async def get_logs(limit: int = 50, level: str = None):
    try:
        params = {"select": "*", "order": "ts.desc", "limit": limit}
        if level:
            params["level"] = f"eq.{level}"
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.get(f"{SUPABASE_URL}/rest/v1/logs", headers=SB_HEADERS, params=params)
            if r.status_code == 200:
                return {"logs": r.json()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"logs": []}


# ── Ping ──

@app.get("/api/ping")
@app.head("/api/ping")
async def ping():
    return {"status": "ok"}


# ── Root ──

@app.get("/")
async def root():
    return FileResponse(BASE_DIR / "index.html")
