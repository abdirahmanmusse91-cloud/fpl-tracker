# FPL Liga Tracker

## Driftsättning på Render.com (gratis)

### Steg 1 – GitHub
1. Skapa ett konto på github.com
2. Skapa ett nytt repository, kalla det `fpl-tracker`
3. Ladda upp alla filer (main.py, requirements.txt, render.yaml, och mappen static/)

### Steg 2 – Render
1. Skapa ett konto på render.com
2. Klicka "New" → "Web Service"
3. Koppla ditt GitHub-konto och välj `fpl-tracker`
4. Render hittar render.yaml automatiskt
5. Lägg till miljövariabel: `ANTHROPIC_API_KEY` = din API-nyckel
6. Klicka "Deploy"

### Steg 3 – Klar!
Du får en länk typ `fpl-tracker.onrender.com` — öppna den på mobilen 🟢

## Lokal körning (för test)
```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=din-nyckel
uvicorn main:app --reload
```
Öppna http://localhost:8000
