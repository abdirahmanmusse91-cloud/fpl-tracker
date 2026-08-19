# Utvecklingsverktyg

Renderar frontend i en riktig webbläsare och testar databasmigrationer mot
riktig Postgres — allt utan root.

## Installation

```bash
./setup.sh
```

Hämtar Chromium (~115 MB, hamnar i `~/.cache/ms-playwright` och ligger kvar),
fyra systembibliotek som WSL-imagen saknar (uppackade i `~/.local/lib/playwright-deps`
i stället för installerade systemvitt) och en emoji-font. Kör den en gång.

Verktygen startas via `run.sh`, som pekar ut biblioteken:

```bash
./run.sh <skript.mjs> [argument]
```

## mobile-shots.mjs

Renderar dashboarden i 390×844 (iPhone), 360×800 (Android) och 320×568 och
skriver skärmbilder. Rapporterar samtidigt varje element vars innehåll är
bredare än sin box — alltså allt som klipps eller spiller över.

```bash
python3 -m uvicorn main:app --port 8940 &
./run.sh mobile-shots.mjs 8940 ./shots efter gw table
#                         port  utkatalog  namn  läge  vy
```

`läge` är ett av `gw total quarter bench hits`, `vy` är `table` eller `compare`.
Sidan matas med deterministisk testdata, så bilder från olika körningar går att
jämföra rakt av.

Det var den här som avslöjade att mobil-CSS:en låg före basreglerna i
stilmallen och därför förlorade kaskaden — chattpanelen behöll `position:sticky`
och lade ~2000 px död scroll under innehållet.

## mobile-inspect.mjs

En viewport, ett läge, plus en mätning av att stapeldiagrammets kolumnrubriker
faktiskt linjerar med sina värden.

```bash
./run.sh mobile-inspect.mjs hits ./shots/minus.png 8940
#                           läge  utfil            port
```

Skriver ut rubrikradens och dataradens kolumngränser i pixlar. Är de olika står
rubrikerna fel — vilket de gjorde i BÄNK och MINUS, med 16 px.

## test-migrations.mjs

Kör `migrations.py` mot Postgres i WASM (PGlite) i stället för mot skarp databas.
Plockar SQL:en direkt ur filen, så testet kör exakt samma text som appen.

```bash
./run.sh test-migrations.mjs
```

Återskapar `gw_cache` som den såg ut före migrationerna, kör alla, kör dem igen
för att bevisa att de är idempotenta, och kontrollerar att förra säsongens och
den nya säsongens GW1 kan samexistera efter att säsongen lagts i primärnyckeln.

Kör den innan varje deploy som innehåller en ny migration.
