"""FPL Liga Tracker — FastAPI backend"""
import os
import time
import math
import asyncio
from pathlib import Path
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
import httpx

BASE_DIR = Path(__file__).parent

app = FastAPI()


@app.on_event("startup")
async def _startup_migrate():
    """Kör väntande databasmigrationer. Får aldrig blockera uppstarten."""
    try:
        from migrations import run_migrations
        print("[migrations]", await run_migrations())
    except Exception as e:
        print(f"[migrations] misslyckades: {e} — fortsätter utan")
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
# FPL uppdaterar ligatabeller live sedan 26/27 — hämta tätare medan omgången spelas
LIVE_GW_TTL = timedelta(minutes=2)
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


async def get_season() -> int:
    """Säsongens startår — 26/27 ger 2026. GW1:s deadline ligger alltid i augusti,
    så årtalet i den identifierar säsongen entydigt."""
    try:
        boot = await get_bootstrap()
        events = boot.get("events") or []
        if events:
            return int(events[0]["deadline_time"][:4])
    except Exception:
        pass
    now = datetime.now(timezone.utc)
    return now.year if now.month >= 7 else now.year - 1


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
    if active is None or active.get("finished", True):
        return False
    # Före deadline har omgången inte börjat — då är den inte "live"
    deadline = active.get("deadline_time")
    if deadline:
        try:
            dt = datetime.fromisoformat(deadline.replace("Z", "+00:00"))
            if datetime.now(timezone.utc) < dt:
                return False
        except ValueError:
            pass
    return True


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
                params={"entry_id": f"eq.{entry_id}", "season": f"eq.{await get_season()}",
                        "select": "gw,points", "order": "gw.asc"},
            )
            if r.status_code == 200:
                return {row["gw"]: row["points"] for row in r.json()}
    except Exception:
        pass
    return {}


# gw_cache behöver kolumnerna points_on_bench, points_on_hits och final
# (se supabase_setup.sql). Saknas någon av dem stängs cachen av helt i stället
# för att servera halva data — appen hämtar då allt färskt från FPL. Vi provar
# igen var 10:e minut så att en körd migration slår igenom utan omstart.
_SCHEMA_OK = True
_SCHEMA_CHECKED_AT = 0.0
_SCHEMA_RETRY = 600


def _schema_usable() -> bool:
    global _SCHEMA_OK
    if not _SCHEMA_OK and (time.time() - _SCHEMA_CHECKED_AT) > _SCHEMA_RETRY:
        _SCHEMA_OK = True
    return _SCHEMA_OK


def _schema_broken(where: str) -> None:
    global _SCHEMA_OK, _SCHEMA_CHECKED_AT
    _SCHEMA_OK = False
    _SCHEMA_CHECKED_AT = time.time()
    print(f"[gw_cache] saknade kolumner ({where}) — cachen avstängd. Sätt DATABASE_URL "
          f"så körs migrationerna automatiskt vid start (se migrations.py)")


async def sb_read_all(entry_ids: list[int]) -> tuple[dict, dict, dict, dict, dict]:
    gw_history: dict[int, dict[int, int]] = {eid: {} for eid in entry_ids}
    bench_history: dict[int, dict[int, int]] = {eid: {} for eid in entry_ids}
    hits_history: dict[int, dict[int, int]] = {eid: {} for eid in entry_ids}
    synced_at_map: dict[int, dict[int, datetime]] = {eid: {} for eid in entry_ids}
    final_map: dict[int, dict[int, bool]] = {eid: {} for eid in entry_ids}
    if not _schema_usable():
        return gw_history, synced_at_map, bench_history, hits_history, final_map
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(
                f"{SUPABASE_URL}/rest/v1/gw_cache",
                headers=SB_HEADERS,
                params={
                    "entry_id": f"in.({','.join(str(e) for e in entry_ids)})",
                    "season": f"eq.{await get_season()}",
                    "select": "entry_id,gw,points,points_on_bench,points_on_hits,synced_at,final",
                },
            )
        if r.status_code == 400:
            _schema_broken("läsning")
        elif r.status_code == 200:
            for row in r.json():
                eid = row["entry_id"]
                gw_history[eid][row["gw"]] = row["points"]
                bench_history[eid][row["gw"]] = row.get("points_on_bench") or 0
                hits_history[eid][row["gw"]] = row.get("points_on_hits") or 0
                final_map[eid][row["gw"]] = bool(row.get("final"))
                raw_ts = row.get("synced_at")
                if raw_ts:
                    try:
                        synced_at_map[eid][row["gw"]] = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
                    except Exception:
                        pass
    except Exception:
        pass
    return gw_history, synced_at_map, bench_history, hits_history, final_map


async def sb_write(entry_id: int, data: dict[int, int], bench_data: dict[int, int] = None,
                   hits_data: dict[int, int] = None, checked_gws: set[int] = None) -> None:
    """final=True först när FPL satt data_checked på omgången. Preliminära poäng
    (omgången är låst först 09:00 UK dagen efter) hämtas om tills dess."""
    if not data:
        return
    now = datetime.now(timezone.utc).isoformat()
    if not _schema_usable():
        return
    checked = checked_gws or set()
    season = await get_season()
    rows = [{"entry_id": entry_id, "season": season, "gw": gw, "points": pts,
             "points_on_bench": (bench_data or {}).get(gw, 0),
             "points_on_hits": (hits_data or {}).get(gw, 0), "synced_at": now,
             "final": gw in checked}
            for gw, pts in data.items()]
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.post(
                f"{SUPABASE_URL}/rest/v1/gw_cache",
                headers={**SB_HEADERS, "Prefer": "resolution=merge-duplicates"},
                json=rows,
            )
            if r.status_code == 400:
                _schema_broken("skrivning")
    except Exception:
        pass


# ── Supabase: managers_cache ──

async def sb_upsert_managers(managers: list, league_id: int = None) -> None:
    now = datetime.now(timezone.utc).isoformat()
    rows = [
        {"entry_id": m["entry"], "player_name": m["player_name"],
         "entry_name": m["entry_name"], "total": m["total"], "rank": i + 1,
         "league_id": league_id, "updated_at": now}
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


async def sb_read_managers(league_id: int = None) -> list:
    try:
        params = {"select": "*", "order": "rank.asc"}
        if league_id:
            params["league_id"] = f"eq.{league_id}"
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.get(
                f"{SUPABASE_URL}/rest/v1/managers_cache",
                headers=SB_HEADERS,
                params=params,
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


# ── Supabase: leagues ──

async def sb_upsert_league(league_id: int, league_name: str) -> None:
    try:
        async with httpx.AsyncClient(timeout=6) as c:
            await c.post(
                f"{SUPABASE_URL}/rest/v1/leagues",
                headers={**SB_HEADERS, "Prefer": "resolution=merge-duplicates"},
                json={"league_id": league_id, "league_name": league_name,
                      "last_used": datetime.now(timezone.utc).isoformat()},
            )
    except Exception:
        pass


async def sb_get_leagues() -> list:
    try:
        async with httpx.AsyncClient(timeout=6) as c:
            r = await c.get(
                f"{SUPABASE_URL}/rest/v1/leagues",
                headers=SB_HEADERS,
                params={"select": "league_id,league_name", "order": "last_used.desc"},
            )
            if r.status_code == 200:
                return r.json()
    except Exception:
        pass
    return []


async def sb_delete_league(league_id: int) -> bool:
    """Tar bort en liga och dess cachade data. gw_cache rörs inte —
    den är nycklad på entry_id och delas mellan ligor."""
    ok = False
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            for table in ("managers_cache", "league_state", "leagues"):
                r = await c.delete(
                    f"{SUPABASE_URL}/rest/v1/{table}",
                    headers=SB_HEADERS,
                    params={"league_id": f"eq.{league_id}"},
                )
                if table == "leagues":
                    ok = r.status_code in (200, 204)
    except Exception:
        pass
    return ok


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
        league_data = league_r.json()
        managers = league_data["standings"]["results"]
        league_name = league_data.get("league", {}).get("name", f"Liga {league_id}")

    # Ny säsong / nyligen anslutna: FPL har tom "standings" tills första GW är
    # beräknad — deltagarna ligger i "new_entries" istället. Lägg till dem med
    # nollställda poäng så dashboarden funkar direkt.
    known = {m["entry"] for m in managers}
    placeholder_entries = set()
    for e in league_data.get("new_entries", {}).get("results", []):
        if e["entry"] in known:
            continue
        placeholder_entries.add(e["entry"])
        managers.append({
            "entry": e["entry"],
            "entry_name": e.get("entry_name", ""),
            "player_name": f'{e.get("player_first_name", "")} {e.get("player_last_name", "")}'.strip(),
            "rank": len(managers) + 1,
            "last_rank": len(managers) + 1,
            "rank_sort": len(managers) + 1,
            "total": 0,
            "event_total": 0,
        })

    entry_ids = [m["entry"] for m in managers]
    gw_history, synced_at_map, bench_history, hits_history, final_map = await sb_read_all(entry_ids)

    now = datetime.now(timezone.utc)
    # Omgångar där FPL bekräftat slutgiltiga poäng — allt annat är preliminärt
    checked_gws = {e["id"] for e in boot.get("events", []) if e.get("data_checked")}

    def ttl_for(gw: int) -> timedelta:
        return LIVE_GW_TTL if (live and gw == current_gw) else CURRENT_GW_TTL

    # needs[entry_id] = omgångar som behöver hämtas från FPL
    needs: dict[int, set[int]] = {}
    for eid in entry_ids:
        cached = gw_history.get(eid, {})
        for gw in gws_to_fetch:
            if gw not in cached:
                needs.setdefault(eid, set()).add(gw)          # saknas helt
            elif not final_map.get(eid, {}).get(gw):
                ts = synced_at_map.get(eid, {}).get(gw)
                if ts is None or (now - ts) > ttl_for(gw):    # preliminär och gammal
                    needs.setdefault(eid, set()).add(gw)

    entries_to_fetch = set(needs.keys())

    if entries_to_fetch:
        to_cache: dict[int, dict[int, int]] = {}
        bench_to_cache: dict[int, dict[int, int]] = {}
        hits_to_cache: dict[int, dict[int, int]] = {}

        async def fetch_entry(entry_id: int):
            try:
                async with httpx.AsyncClient(timeout=15) as c:
                    r = await c.get(f"{FPL_BASE}/entry/{entry_id}/history/", headers=FPL_HEADERS)
                    data = r.json()
                new_data: dict[int, int] = {}
                new_bench: dict[int, int] = {}
                new_hits: dict[int, int] = {}
                wanted = needs.get(entry_id, set())
                for gw_row in data.get("current", []):
                    gw_num = gw_row["event"]
                    hit_cost = gw_row.get("event_transfers_cost") or 0
                    pts = gw_row["points"] - hit_cost
                    bench = gw_row.get("points_on_bench") or 0
                    if gw_num in wanted:
                        gw_history[entry_id][gw_num] = pts
                        bench_history[entry_id][gw_num] = bench
                        hits_history[entry_id][gw_num] = hit_cost
                        new_data[gw_num] = pts
                        new_bench[gw_num] = bench
                        new_hits[gw_num] = hit_cost
                if new_data:
                    to_cache[entry_id] = new_data
                    bench_to_cache[entry_id] = new_bench
                    hits_to_cache[entry_id] = new_hits
            except Exception:
                pass

        await asyncio.gather(*[fetch_entry(eid) for eid in entries_to_fetch])
        for eid, data in to_cache.items():
            background_tasks.add_task(sb_write, eid, data, bench_to_cache.get(eid),
                                      hits_to_cache.get(eid), checked_gws)

    # Placeholder-managers saknar total från FPL — räkna ut den från historiken
    # och sortera om tabellen så ställningen stämmer redan under GW1.
    if placeholder_entries:
        for m in managers:
            if m["entry"] in placeholder_entries:
                hist = gw_history.get(m["entry"], {})
                m["total"] = sum(hist.values())
                m["event_total"] = hist.get(current_gw, 0)
        managers.sort(key=lambda m: -m["total"])
        for i, m in enumerate(managers):
            m["rank"] = i + 1

    background_tasks.add_task(sb_upsert_managers, managers, league_id)
    background_tasks.add_task(sb_update_league_state, league_id, current_gw, live, managers)
    background_tasks.add_task(sb_upsert_league, league_id, league_name)

    return {
        "managers": managers,
        "gw_history": gw_history,
        "bench_history": bench_history,
        "hits_history": hits_history,
        "current_gw": current_gw,
        "is_live": live,
        "league_name": league_name,
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
    checked_gws = {e["id"] for e in boot.get("events", []) if e.get("data_checked")}
    background_tasks.add_task(sb_write, entry_id, to_cache, None, None, checked_gws)
    return data


# ── Bootstrap proxy ──

@app.get("/api/bootstrap")
async def bootstrap_proxy():
    return await get_bootstrap()


# ── Team price view ──

@app.get("/api/team/{entry_id}/prices")
async def get_team_prices(entry_id: int, gw: int | None = None):
    boot = await get_bootstrap()
    _, current_gw = get_finished_gws(boot)
    requested_gw = gw if gw is not None else current_gw
    is_current   = requested_gw == current_gw

    players_map = {p["id"]: p for p in boot["elements"]}
    teams_map   = {t["id"]: t["short_name"] for t in boot["teams"]}

    async with httpx.AsyncClient(timeout=20) as c:
        picks_r, transfers_r = await asyncio.gather(
            c.get(f"{FPL_BASE}/entry/{entry_id}/event/{requested_gw}/picks/", headers=FPL_HEADERS),
            c.get(f"{FPL_BASE}/entry/{entry_id}/transfers/", headers=FPL_HEADERS),
        )

    if picks_r.status_code != 200:
        raise HTTPException(status_code=picks_r.status_code, detail="Kunde inte hämta truppen")

    picks     = picks_r.json().get("picks", [])
    transfers = transfers_r.json() if transfers_r.status_code == 200 else []
    pick_ids  = [p["element"] for p in picks]

    # Only transfers at or before requested_gw
    transfer_map: dict[int, dict] = {}
    for t in transfers:
        if t["event"] > requested_gw:
            continue
        el = t["element_in"]
        if el not in transfer_map or t["event"] > transfer_map[el]["gw"]:
            transfer_map[el] = {"price": t["element_in_cost"], "gw": t["event"]}

    # Prices at requested_gw
    if is_current:
        gw_price    = {el: players_map[el]["now_cost"] for el in pick_ids if el in players_map}
        start_price = {}  # use cost_change_start fallback below
    else:
        # Fetch element-summary for each player in parallel
        async with httpx.AsyncClient(timeout=20) as c:
            responses = await asyncio.gather(*[
                c.get(f"{FPL_BASE}/element-summary/{el}/", headers=FPL_HEADERS)
                for el in pick_ids
            ])
        gw_price    = {}
        start_price = {}
        for el, r in zip(pick_ids, responses):
            if r.status_code != 200:
                continue
            history = r.json().get("history", [])
            at_req = next((h["value"] for h in history if h["round"] == requested_gw), None)
            at_gw1 = next((h["value"] for h in history if h["round"] == 1), None)
            if at_req is not None:
                gw_price[el] = at_req
            if at_gw1 is not None:
                start_price[el] = at_gw1

    pos_label = {1: "MV", 2: "DEF", 3: "MID", 4: "FWD"}
    pos_order = {1: 0, 2: 1, 3: 2, 4: 3}

    result = []
    for pick in picks:
        el_id  = pick["element"]
        player = players_map.get(el_id, {})
        price  = gw_price.get(el_id, player.get("now_cost", 0))

        if el_id in transfer_map:
            buy_price = transfer_map[el_id]["price"]
            buy_gw    = transfer_map[el_id]["gw"]
        elif is_current:
            buy_price = player.get("now_cost", 0) - player.get("cost_change_start", 0)
            buy_gw    = None
        else:
            buy_price = start_price.get(el_id, price)
            buy_gw    = None

        if price > buy_price:
            sell_price = buy_price + math.floor((price - buy_price) / 2)
        else:
            sell_price = price
        profit = sell_price - buy_price

        result.append({
            "element_id": el_id,
            "name":       player.get("web_name", "?"),
            "position":   pos_label.get(player.get("element_type", 0), "?"),
            "pos_order":  pos_order.get(player.get("element_type", 0), 4),
            "team":       teams_map.get(player.get("team", 0), "?"),
            "buy_price":  buy_price,
            "buy_gw":     buy_gw,
            "now_cost":   price,
            "sell_price": sell_price,
            "change":     profit,
        })

    result.sort(key=lambda x: (x["pos_order"], x["name"]))
    for p in result:
        del p["pos_order"]

    return {
        "players":      result,
        "net_change":   sum(p["change"] for p in result),
        "current_gw":   current_gw,
        "requested_gw": requested_gw,
    }


# ── Sync endpoint ──

@app.post("/api/sync")
async def sync_all():
    boot = await get_bootstrap()
    all_finished, current_gw = get_finished_gws(boot)
    historical_set = set(gw for gw in all_finished if gw < current_gw)

    if not historical_set:
        return {"message": "No historical GWs to sync", "synced": 0}

    checked_gws = {e["id"] for e in boot.get("events", []) if e.get("data_checked")}

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
            await sb_write(entry_id, to_cache, None, None, checked_gws)
            synced += 1
        except Exception:
            pass

    await asyncio.gather(*[sync_entry(eid) for eid in entries])
    return {"synced": synced, "total": len(entries), "gws_cached": sorted(historical_set)}


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

@app.get("/api/leagues")
async def get_leagues():
    return await sb_get_leagues()


@app.delete("/api/leagues/{league_id}")
async def delete_league(league_id: int):
    ok = await sb_delete_league(league_id)
    if not ok:
        raise HTTPException(status_code=502, detail="Kunde inte ta bort ligan")
    return {"deleted": league_id}


@app.get("/api/livecheck/{league_id}")
async def livecheck(league_id: int):
    """Jämför de tre möjliga källorna för aktuell omgångs poäng.

    Körs mitt under en pågående omgång för att avgöra vilka som uppdateras live:
      standings_event_total — ur ligatabellen, samma endpoint FPL:s egen
                              live-uppdaterade tabell bygger på
      history_points        — ur /entry/{id}/history/, det appen använder idag
      picks_live_points     — uträknat ur truppen + /event/{gw}/live/, garanterat
                              live men kräver egen poänglogik
    Stämmer de tre överens under pågående matcher är alla live.
    """
    boot = await get_bootstrap()
    _, current_gw = get_finished_gws(boot)

    async with httpx.AsyncClient(timeout=25) as c:
        lr = await c.get(f"{FPL_BASE}/leagues-classic/{league_id}/standings/", headers=FPL_HEADERS)
        if lr.status_code != 200:
            raise HTTPException(status_code=lr.status_code, detail="FPL API error")
        data = lr.json()
        managers = data["standings"]["results"]

        live_r = await c.get(f"{FPL_BASE}/event/{current_gw}/live/", headers=FPL_HEADERS)
        pts_by_player = {e["id"]: e["stats"]["total_points"]
                         for e in live_r.json().get("elements", [])}

        async def per_entry(m: dict) -> dict:
            eid = m["entry"]
            hist_pts = picks_pts = None
            try:
                hr = await c.get(f"{FPL_BASE}/entry/{eid}/history/", headers=FPL_HEADERS)
                row = next((g for g in hr.json().get("current", []) if g["event"] == current_gw), None)
                if row:
                    hist_pts = row["points"] - (row.get("event_transfers_cost") or 0)
            except Exception:
                pass
            try:
                pr = await c.get(f"{FPL_BASE}/entry/{eid}/event/{current_gw}/picks/", headers=FPL_HEADERS)
                pj = pr.json()
                # multiplier är 0 på bänken, 2 för kapten, 3 för triple captain
                picks_pts = sum(p["multiplier"] * pts_by_player.get(p["element"], 0)
                                for p in pj.get("picks", []))
                picks_pts -= (pj.get("entry_history") or {}).get("event_transfers_cost", 0)
            except Exception:
                pass
            return {"manager": m.get("player_name"),
                    "standings_event_total": m.get("event_total"),
                    "standings_total": m.get("total"),
                    "history_points": hist_pts,
                    "picks_live_points": picks_pts}

        rows = await asyncio.gather(*[per_entry(m) for m in managers])

    return {
        "current_gw": current_gw,
        "is_live": is_gw_live(boot, current_gw),
        "last_updated_data": data.get("last_updated_data"),
        "standings_rader": len(managers),
        "spelare_i_live_endpoint": len(pts_by_player),
        "managers": rows,
    }


@app.get("/api/ping")
@app.head("/api/ping")
async def ping():
    return {"status": "ok"}


@app.get("/api/healthz")
@app.head("/api/healthz")
async def healthz():
    """Pingar Supabase så att projektet inte pausas."""
    await sb_get_leagues()
    return {"status": "ok"}


# ── Root ──

@app.get("/")
async def root():
    return FileResponse(BASE_DIR / "index.html")
