import { chromium } from 'playwright';

const NAMES = [
  ['Malik Himber','Tornado'], ['Daniel Sabo','DS'], ['Ermias Abraham','Mister x'],
  ['Simon Habtemicael','Winner FC'], ['Robel Asfaha','The Special One'],
  ['Abdirahman Musse','Netflix FC'], ['Yonas Tesfay','Kung Fu Panda'], ['Mikael Berhane','Los Blancos'],
];
const CURRENT_GW = 12;

// deterministisk testdata så bilderna går att jämföra mellan körningar
function makeData() {
  let seed = 7;
  const rnd = (n) => (seed = (seed * 1103515245 + 12345) % 2147483648) % n;
  const managers = [], gwHistory = {}, benchHistory = {}, hitsHistory = {};
  NAMES.forEach(([player_name, entry_name], i) => {
    const entry = 100000 + i;
    let total = 0;
    gwHistory[entry] = {}; benchHistory[entry] = {}; hitsHistory[entry] = {};
    for (let g = 1; g <= CURRENT_GW; g++) {
      const p = 30 + rnd(60);
      gwHistory[entry][g] = p; total += p;
      benchHistory[entry][g] = rnd(18);
      hitsHistory[entry][g] = rnd(5) === 0 ? 4 : 0;
    }
    managers.push({ entry, player_name, entry_name, total, rank: i + 1, event_total: gwHistory[entry][CURRENT_GW] });
  });
  managers.sort((a, b) => b.total - a.total);
  managers.forEach((m, i) => m.rank = i + 1);
  return { managers, gwHistory, benchHistory, hitsHistory };
}

const [,, port, outDir, label, mode, view] = process.argv;
const browser = await chromium.launch();
const VIEWPORTS = [['iphone15', 390, 844], ['android', 360, 800], ['smal', 320, 568]];
const report = [];

for (const [vpName, width, height] of VIEWPORTS) {
  const page = await browser.newPage({ viewport: { width, height }, deviceScaleFactor: 2 });
  await page.goto(`http://127.0.0.1:${port}/`, { waitUntil: 'networkidle' });
  await page.evaluate(({ data, gw, mode, view }) => {
    managers = data.managers; gwHistory = data.gwHistory;
    benchHistory = data.benchHistory; hitsHistory = data.hitsHistory;
    currentGW = gw; selectedGW = gw; dataLoaded = true;
    document.getElementById('setup-overlay').classList.add('hidden');
    document.getElementById('loading').classList.add('hidden');
    document.getElementById('dashboard').classList.remove('hidden');
    document.getElementById('gw-pill').classList.remove('hidden');
    document.getElementById('gw-pill').textContent = `GW ${gw}`;
    document.getElementById('live-badge').classList.remove('hidden');
    document.getElementById('league-name-sub').textContent = 'Big Blinds 26/27';
    render();
    if (mode && mode !== 'gw') setBarMode(mode);
    if (view) setMobileView(view);
  }, { data: makeData(), gw: CURRENT_GW, mode, view });

  // mät överflöd: vilka element är bredare än sin egen box?
  const overflow = await page.evaluate(() => {
    const bad = [];
    document.querySelectorAll('*').forEach(el => {
      if (el.scrollWidth > el.clientWidth + 1 && el.clientWidth > 0) {
        const cs = getComputedStyle(el);
        if (cs.overflowX === 'visible' || cs.overflowX === 'hidden') {
          bad.push({ sel: el.className || el.tagName, over: el.scrollWidth - el.clientWidth });
        }
      }
    });
    return { bodyScroll: document.documentElement.scrollWidth, win: window.innerWidth, bad: bad.slice(0, 8) };
  });
  report.push({ vpName, width, ...overflow });
  await page.screenshot({ path: `${outDir}/${label}-${vpName}.png`, fullPage: true });
  await page.close();
}
await browser.close();
console.log(JSON.stringify(report, null, 1));
