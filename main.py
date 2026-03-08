from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
import httpx
import os

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

FPL_BASE = "https://fantasy.premierleague.com/api"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
}

@app.get("/api/league/{league_id}")
async def get_league(league_id: int):
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(f"{FPL_BASE}/leagues-classic/{league_id}/standings/", headers=HEADERS)
        if r.status_code != 200:
            raise HTTPException(status_code=r.status_code, detail="FPL API error")
        return r.json()

@app.get("/api/bootstrap")
async def get_bootstrap():
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(f"{FPL_BASE}/bootstrap-static/", headers=HEADERS)
        if r.status_code != 200:
            raise HTTPException(status_code=r.status_code, detail="FPL API error")
        return r.json()

@app.get("/api/entry/{entry_id}/history")
async def get_history(entry_id: int):
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(f"{FPL_BASE}/entry/{entry_id}/history/", headers=HEADERS)
        if r.status_code != 200:
            raise HTTPException(status_code=r.status_code, detail="FPL API error")
        return r.json()

@app.post("/api/chat")
async def chat(body: dict):
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="GROQ_API_KEY saknas")

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": "llama-3.3-70b-versatile",
                "messages": [
                    {"role": "system", "content": body.get("system", "Du är en FPL-expert. Svara på svenska.")},
                    {"role": "user", "content": body.get("message", "")}
                ],
                "max_tokens": 500
            }
        )
        if r.status_code != 200:
            raise HTTPException(status_code=r.status_code, detail=f"Groq API error: {r.text}")
        data = r.json()
        return {"reply": data["choices"][0]["message"]["content"]}

@app.get("/")
async def root():
    return FileResponse("index.html")
