import { chromium } from 'playwright';
const [,, mode, out, port = '8940'] = process.argv;
const b = await chromium.launch();
const p = await b.newPage({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2 });
await p.goto(`http://127.0.0.1:${port}/`, { waitUntil: 'networkidle' });
await p.evaluate((mode) => {
  const NAMES = [['Malik Himber','Tornado'],['Daniel Sabo','DS'],['Ermias Abraham','Mister x'],
    ['Simon Habtemicael','Winner FC'],['Robel Asfaha','The Special One'],['Abdirahman Musse','Netflix FC'],
    ['Yonas Tesfay','Kung Fu Panda'],['Mikael Berhane','Los Blancos']];
  let seed = 7; const rnd = n => (seed = (seed*1103515245+12345) % 2147483648) % n;
  managers = []; gwHistory = {}; benchHistory = {}; hitsHistory = {};
  NAMES.forEach(([pn, en], i) => {
    const entry = 100000+i; let total = 0;
    gwHistory[entry]={}; benchHistory[entry]={}; hitsHistory[entry]={};
    for (let g=1; g<=12; g++) { const v=30+rnd(60); gwHistory[entry][g]=v; total+=v;
      benchHistory[entry][g]=rnd(18); hitsHistory[entry][g]= rnd(3)===0 ? 4 : 0; }
    managers.push({entry, player_name:pn, entry_name:en, total, rank:i+1});
  });
  managers.sort((a,b)=>b.total-a.total);
  currentGW=12; selectedGW=12; dataLoaded=true;
  document.getElementById('setup-overlay').classList.add('hidden');
  document.getElementById('loading').classList.add('hidden');
  document.getElementById('dashboard').classList.remove('hidden');
  document.getElementById('league-name-sub').textContent='Big Blinds 26/27';
  document.getElementById('gw-pill').classList.remove('hidden');
  document.getElementById('gw-pill').textContent='GW 12';
  document.getElementById('live-badge').classList.remove('hidden');
  render(); if (mode!=='gw') setBarMode(mode);
}, mode);
// mät rubrikradens kolumner mot första datradens
const align = await p.evaluate(() => {
  const head = document.querySelector('.bar-head-row');
  const row  = document.querySelectorAll('.bar-row')[0];
  const box = el => { const r = el.getBoundingClientRect(); return `${Math.round(r.left)}–${Math.round(r.right)}`; };
  return {
    rubrik: head ? [...head.children].map(box) : 'ingen rubrikrad',
    datarad: row ? [...row.children].map(box) : 'ingen datarad',
    rubriktext: head ? head.innerText.replace(/\n/g,' | ') : '',
  };
});
console.log(JSON.stringify(align, null, 1));
await p.screenshot({ path: out });
await b.close();
