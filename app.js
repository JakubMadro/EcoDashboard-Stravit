/* =====================================================================
   Dane live pobieramy z lokalnego proxy server.py:
   /api/challenge/rywalizacja-sportowa/data

   CSV zostaje tylko jako awaryjny fallback, gdy sesja Stravit wygasnie
   albo dashboard zostanie otwarty bez lokalnego serwera.
   ===================================================================== */
async function loadCSV(url) {
  const resp = await fetch(url);
  if (!resp.ok) throw new Error(`Nie można załadować CSV: ${resp.status}`);
  return await resp.text();
}

function parseTimeToSeconds(timeStr) {
  // format: HH:MM:SS
  const parts = timeStr.trim().split(':');
  if (parts.length !== 3) return 0;
  return parseInt(parts[0]) * 3600 + parseInt(parts[1]) * 60 + parseInt(parts[2]);
}

function buildDATA(csvText) {
  const lines = csvText.split('\n');

  // Znajdź linię nagłówkową (zawiera "nazwa uzytkownika" lub "lp")
  let headerIdx = -1;
  for (let i = 0; i < lines.length; i++) {
    const low = lines[i].toLowerCase();
    if (low.includes('nazwa uzytkownika') || low.includes('przyznane_punkty')) {
      headerIdx = i;
      break;
    }
  }
  if (headerIdx === -1) throw new Error('Nie znaleziono nagłówka CSV');

  // Wiersze danych
  const dataLines = lines.slice(headerIdx + 1).filter(l => l.trim() && l.trim() !== 'aaa');

  // Kolekcja aktywności
  const activities = [];
  for (const line of dataLines) {
    const cols = line.split(';').map(c => c.trim());
    if (cols.length < 9) continue;
    const name   = cols[1];
    const dist   = parseFloat(cols[2].replace(',', '.')) || 0;
    const pts    = parseFloat(cols[3].replace(',', '.')) || 0;
    const elev   = parseFloat(cols[4].replace(',', '.')) || 0;
    const timeSec = parseTimeToSeconds(cols[5]);
    const type   = cols[6];
    const dateStr = cols[8].substring(0, 10); // YYYY-MM-DD
    if (!name || !dateStr) continue;
    activities.push({ name, dist, pts, elev, timeSec, type, dateStr });
  }

  if (activities.length === 0) throw new Error('Brak wierszy w CSV');

  // Wykryj zakres dat
  const allDatesSet = new Set(activities.map(a => a.dateStr));
  const allDates = Array.from(allDatesSet).sort();
  const dateRange = [allDates[0], allDates[allDates.length - 1]];

  // Buduj per-user stats
  const usersMap = {};
  for (const act of activities) {
    if (!usersMap[act.name]) {
      usersMap[act.name] = {
        distance: 0, points: 0, elevation: 0, time: 0, count: 0,
        daily: {}, byType: {}
      };
      // Inicjalizuj zerami dla wszystkich dat zakresu
      for (const d of allDates) {
        usersMap[act.name].daily[d] = { points: 0, distance: 0 };
      }
    }
    const u = usersMap[act.name];
    u.distance += act.dist;
    u.points   += act.pts;
    u.elevation += act.elev;
    u.time     += act.timeSec;
    u.count    += 1;

    if (u.daily[act.dateStr]) {
      u.daily[act.dateStr].points   += act.pts;
      u.daily[act.dateStr].distance += act.dist;
    }
    // Jeśli data aktywności nie jest w głównym zakresie — ignoruj (nie crashuj)

    if (!u.byType[act.type]) {
      u.byType[act.type] = { count: 0, distance: 0, points: 0, time: 0 };
    }
    u.byType[act.type].count    += 1;
    u.byType[act.type].distance += act.dist;
    u.byType[act.type].points   += act.pts;
    u.byType[act.type].time     += act.timeSec;
  }

  // Zaokrągl wartości
  for (const u of Object.values(usersMap)) {
    u.distance = Math.round(u.distance * 100) / 100;
    u.points   = Math.round(u.points   * 100) / 100;
    u.elevation = Math.round(u.elevation * 10) / 10;
    for (const d of Object.values(u.daily)) {
      d.points   = Math.round(d.points   * 100) / 100;
      d.distance = Math.round(d.distance * 100) / 100;
    }
    for (const t of Object.values(u.byType)) {
      t.distance = Math.round(t.distance * 100) / 100;
      t.points   = Math.round(t.points   * 100) / 100;
    }
  }

  // Ranking po punktach
  const sorted = Object.entries(usersMap)
    .sort(([,a],[,b]) => b.points - a.points);
  sorted.forEach(([name, u], idx) => { u.rank = idx + 1; });

  // Top 10 liderów
  const topLeaders = sorted.slice(0, 10).map(([name, u]) => ({
    name, points: u.points, rank: u.rank
  }));

  // Totals
  const totals = {
    distance: Math.round(Object.values(usersMap).reduce((s, u) => s + u.distance, 0) * 10) / 10,
    points:   Math.round(Object.values(usersMap).reduce((s, u) => s + u.points,   0) * 10) / 10,
    count:    activities.length,
    time:     Object.values(usersMap).reduce((s, u) => s + u.time, 0),
  };

  const allNames = Object.keys(usersMap).sort((a, b) => a.localeCompare(b, 'pl'));
  const totalUsers = allNames.length;

  return { dateRange, allDates, totalUsers, totals, allNames, topLeaders, users: usersMap };
}

// Nazwa pliku CSV — ostatnia awaryjna kopia, gdy API/proxy nie działa
const CSV_FILENAME = 'rywalizacja-sportowa-activities-2026-07-06_14_05_56.csv';

const CHALLENGE_SLUG = 'rywalizacja-sportowa';

let DATA = null; // zostanie wypełnione po załadowaniu API

async function fetchApiData(force = false) {
  const crewQuery = crew.join(',');
  const url = `/api/v1/challenge/${CHALLENGE_SLUG}/data?` +
              (force ? 'force=true&' : '') +
              (crewQuery ? `crew=${encodeURIComponent(crewQuery)}` : '');
  const resp = await fetch(url, {
    method: 'GET',
    cache: 'no-store',
    headers: { 'accept': 'application/json' }
  });
  const payload = await resp.json().catch(() => null);
  if (!resp.ok || !payload) {
    const err = new Error(payload?.error || `HTTP ${resp.status}`);
    err.authRequired = Boolean(payload?.authRequired || resp.status === 401);
    throw err;
  }
  if (!payload.users || !payload.dateRange) {
    throw new Error('API zwrocilo niepelny zestaw danych.');
  }
  return payload;
}

function setAuthPanelVisible(visible) {
  const panel = document.getElementById('authPanel');
  if (panel) panel.classList.toggle('show', visible);
}

async function loginToStravit(event) {
  if (event) event.preventDefault();
  const email = document.getElementById('authEmail');
  const password = document.getElementById('authPassword');
  const btn = document.getElementById('authBtn');
  const msg = document.getElementById('authMsg');
  if (!email.value.trim() || !password.value) {
    if (msg) { msg.textContent = 'Podaj email i hasło.'; msg.style.color = 'var(--coral)'; }
    return;
  }
  if (btn) { btn.disabled = true; btn.textContent = 'Logowanie…'; }
  if (msg) { msg.textContent = ''; msg.style.color = 'var(--muted)'; }
  try {
    const resp = await fetch('/api/v1/auth/login', {
      method: 'POST',
      headers: { 'content-type': 'application/json', 'accept': 'application/json' },
      body: JSON.stringify({ email: email.value.trim(), password: password.value, remember: true })
    });
    const payload = await resp.json().catch(() => null);
    if (!resp.ok) throw new Error(payload?.error || `HTTP ${resp.status}`);
    password.value = '';
    setAuthPanelVisible(false);
    if (msg) msg.textContent = '';
    await refreshData(false);
  } catch(err) {
    if (msg) { msg.textContent = err.message; msg.style.color = 'var(--coral)'; }
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Zaloguj'; }
  }
}

async function refreshData(force = false) {
  const btn = document.getElementById('refreshBtn');
  const status = document.getElementById('refreshStatus');
  if (btn) { btn.disabled = true; btn.textContent = '⟳ Pobieranie…'; }
  if (status) { status.textContent = ''; status.style.color = 'var(--muted)'; }
  try {
    const activeId = profileId || crewId;

    // Definiujemy obietnice dla równoległego pobierania
    const dataPromise = fetchApiData(force);
    const crewPromise = fetch(`/api/v1/crew?id=${encodeURIComponent(activeId)}`)
      .then(r => r.ok ? r.json() : null)
      .catch(() => null);
    const profilesPromise = fetch(`/api/v1/crew/profiles`)
      .then(r => r.ok ? r.json() : [])
      .catch(() => []);

    let apiData;
    try {
      const [resolvedData, resolvedCrew, resolvedProfiles] = await Promise.all([
        dataPromise,
        crewPromise,
        profilesPromise
      ]);
      
      apiData = resolvedData;
      DATA = apiData;
      if (status) status.textContent = '✓ Stravit · ' + new Date().toLocaleTimeString('pl-PL', {hour:'2-digit',minute:'2-digit'});
      setAuthPanelVisible(false);
      try { localStorage.setItem('dashboard-cached-data', JSON.stringify(DATA)); } catch(e){}

      if (resolvedProfiles) {
        savedProfiles = resolvedProfiles;
      }

      if (resolvedCrew) {
        if (activeId === profileId) {
          applyProfile(resolvedCrew, false);
        } else {
          crew = Array.isArray(resolvedCrew) ? resolvedCrew : (resolvedCrew.members || []);
        }
      }
    } catch(apiErr) {
      console.warn('API fetch failed, falling back:', apiErr.message);
      if (apiErr.authRequired) setAuthPanelVisible(true);
      
      try {
        const [resolvedCrew, resolvedProfiles] = await Promise.all([crewPromise, profilesPromise]);
        if (resolvedCrew) {
          crew = Array.isArray(resolvedCrew) ? resolvedCrew : (resolvedCrew.members || []);
        }
        if (resolvedProfiles) savedProfiles = resolvedProfiles;
      } catch(e) {}

      if (!DATA) {
        let csvText = await loadCSV(CSV_FILENAME);
        DATA = buildDATA(csvText);
      }
      if (status) { status.textContent = '⚠ zaloguj Stravit · ostatnie dane'; status.style.color = 'var(--amber)'; }
    }
    
    updateHeader();
    renderDatalist();
    renderProfileControls();

    loadCrew();
    crew = crew.filter(n => DATA.users[n]);
    if (ME_NAME && !crew.includes(ME_NAME)) {
      crew.unshift(ME_NAME);
    }
    await saveCrew();
    renderAll();
  } catch(err) {
    if (status) { status.textContent = '✗ ' + err.message; status.style.color = 'var(--coral)'; }
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '↻ Odśwież dane'; }
  }
}

function updateHeader() {
  const sub = document.getElementById('subtitleText');
  if (sub) sub.innerHTML = `${fmtDateShort(DATA.dateRange[0])} – ${fmtDateShort(DATA.dateRange[1])} &nbsp;·&nbsp; <b>${DATA.totalUsers}</b> zawodników w pełnej klasyfikacji`;
  
  // Zaktualizuj plakietkę aktywnego użytkownika (JA)
  const badge = document.getElementById('currentUserBadge');
  if (badge) {
    if (ME_NAME) {
      badge.innerHTML = `👤 JA: <b>${ME_NAME}</b>`;
      badge.style.display = 'inline-flex';
    } else {
      badge.style.display = 'none';
    }
  }

  [['tv0', DATA.totals.distance.toLocaleString('pl-PL')],
   ['tv1', DATA.totals.count.toLocaleString('pl-PL')],
   ['tv2', Math.round(DATA.totals.points).toLocaleString('pl-PL')],
   ['tv3', fmtTime(DATA.totals.time)]
   ].forEach(([id,val]) => { const el = document.getElementById(id); if(el) el.textContent = val; });

  const footer = document.getElementById('dashboardFooter');
  if (footer && DATA.dateRange) {
    footer.textContent = `EKSPORT WYZWANIA · ${fmtDateShort(DATA.dateRange[0]).toUpperCase()} – ${fmtDateShort(DATA.dateRange[1]).toUpperCase()} · ${DATA.totalUsers} UCZESTNIKÓW · AUTOR: JAKUB MĄDRO`;
  }
}

// ==================== SCRIPT BLOCK ====================

const PALETTE = ['#ffb700','#2ec4b6','#ef476f','#38bdf8','#a78bfa','#fb923c','#94e2d5','#f472b6'];
let ME_NAME = localStorage.getItem('dashboard-me') || '';
let crewId = localStorage.getItem('dashboard-crew-id');
if (!crewId) {
  crewId = 'crew_' + Math.random().toString(36).substring(2, 15) + Math.random().toString(36).substring(2, 15);
  localStorage.setItem('dashboard-crew-id', crewId);
}
let profileId = localStorage.getItem('dashboard-profile-id') || '';
let savedProfiles = [];
const TYPE_LABELS = {Run:'Bieganie', Walk:'Marsz', Ride:'Kolarstwo', VirtualRide:'Kolarstwo (trenażer)', Hike:'Wędrówka', WeightTraining:'Siłownia', Swim:'Pływanie', Workout:'Trening', InlineSkate:'Rolki'};
const TYPE_COLORS = {Run:'#ef476f', Walk:'#ffb700', Ride:'#2ec4b6', VirtualRide:'#0891a4', Hike:'#a78bfa', WeightTraining:'#f472b6', Swim:'#38bdf8', Workout:'#fb923c', InlineSkate:'#94a3b8'};

let crew = [];
let charts = {};
let profileCollapsed = localStorage.getItem('dashboard-profile-collapsed') === 'true';

function getCrewStorageKey(){
  return profileId ? `tracked-athletes-${profileId}` : 'tracked-athletes';
}

function resolveName(value){
  const trimmed = (value || '').trim();
  const names = ALL_NAMES || [];
  if (!trimmed || !names.length) return null;
  const exact = names.find(n => n.toLowerCase() === trimmed.toLowerCase());
  if (exact) return exact;
  const close = names.filter(n => n.toLowerCase().includes(trimmed.toLowerCase())).slice(0, 3);
  return close[0] || null;
}

function renderProfileControls(){
  const profileInput = document.getElementById('profileInput');
  const meInput = document.getElementById('meInput');
  
  if (profileInput) {
    profileInput.value = ''; // Always empty for clean search
    profileInput.placeholder = ME_NAME ? `Profil: ${ME_NAME}` : 'Szukaj profilu…';
  }
  if (meInput) {
    meInput.value = ''; // Always clean for new profile creation
  }
  
  const listWrap = document.getElementById('profileListWrap');
  if (listWrap) {
    const recents = JSON.parse(localStorage.getItem('dashboard-recent-profile-ids') || '[]');
    const visibleProfiles = savedProfiles.filter(p => recents.includes(p.id) || p.id === profileId);

    if (!visibleProfiles.length) {
      listWrap.innerHTML = '<div class="profile-empty">Wyszukaj lub stwórz profil powyżej, aby zapisać go na tym urządzeniu.</div>';
    } else {
      listWrap.innerHTML = '<div style="font-size: 11px; color: var(--muted); margin-bottom: 6px; width: 100%; font-weight: 600; letter-spacing: 0.03em; text-transform: uppercase;">Ostatnio używane:</div>' + 
        visibleProfiles.map(p => {
          const active = p.id === profileId;
          return `<button type="button" class="profile-pill${active?' active':''}" data-id="${p.id}">${(p.me || 'Osoba').replace(/</g,'&lt;')}</button>`;
        }).join('');
      
      Array.from(listWrap.querySelectorAll('.profile-pill')).forEach(btn => {
        btn.onclick = async () => {
          const id = btn.getAttribute('data-id');
          await loadProfileById(id);
        };
      });
    }
  }
}

function applyProfile(profile, triggerApiRefresh = true){
  if (!profile) return;
  profileId = profile.id || '';
  ME_NAME = profile.me || '';
  localStorage.setItem('dashboard-profile-id', profileId);
  localStorage.setItem('dashboard-me', ME_NAME);
  
  // Zapamiętaj ostatnio używane profile na tym urządzeniu
  let recents = JSON.parse(localStorage.getItem('dashboard-recent-profile-ids') || '[]');
  recents = recents.filter(id => id !== profileId);
  recents.unshift(profileId);
  recents = recents.slice(0, 3);
  localStorage.setItem('dashboard-recent-profile-ids', JSON.stringify(recents));

  const members = Array.isArray(profile.members) ? profile.members.filter(Boolean) : [];
  crew = members.filter(n => DATA?.users?.[n]);
  if (ME_NAME && !crew.includes(ME_NAME) && DATA?.users?.[ME_NAME]) {
    crew.unshift(ME_NAME);
  }
  localStorage.setItem(getCrewStorageKey(), JSON.stringify(crew));
  renderProfileControls();
  
  if (triggerApiRefresh) {
    refreshData(false);
  } else {
    renderAll();
  }
}

async function loadSavedProfiles(){
  try {
    const resp = await fetch('/api/v1/crew/profiles');
    if (!resp.ok) throw new Error('profiles');
    const data = await resp.json();
    savedProfiles = Array.isArray(data) ? data.filter(p => p && p.me && p.me.trim()) : [];
    renderProfileControls();
  } catch (e) {
    savedProfiles = [];
  }
}

async function createProfile(){
  const meInput = document.getElementById('meInput');
  const profileMsg = document.getElementById('profileMsg');
  const meValue = (meInput?.value || '').trim();
  if (!meValue) {
    if (profileMsg) {
      profileMsg.textContent = 'Wpisz nazwę zawodnika, który ma być Twoim profilem.';
      profileMsg.className = 'add-msg err';
    }
    return;
  }

  const resolvedMe = resolveName(meValue) || meValue;
  if (!resolvedMe) {
    if (profileMsg) {
      profileMsg.textContent = 'Nie znalazłem takiej osoby w danych wyzwania.';
      profileMsg.className = 'add-msg err';
    }
    return;
  }

  await loadSavedProfiles();
  const duplicate = savedProfiles.find(p => (p.me || '').trim().toLowerCase() === resolvedMe.trim().toLowerCase());
  if (duplicate && duplicate.id !== profileId) {
    if (profileMsg) {
      profileMsg.textContent = `Profil dla ${resolvedMe} już istnieje.`;
      profileMsg.className = 'add-msg err';
    }
    return;
  }

  profileId = `profile_${Date.now().toString(36)}`;
  ME_NAME = resolvedMe;
  crew = DATA.users?.[ME_NAME] ? [ME_NAME] : [];
  await saveCrew();
  await loadSavedProfiles();
  renderProfileControls();
  if (profileMsg) {
    profileMsg.textContent = `Profil dla ${ME_NAME} utworzony.`;
    profileMsg.className = 'add-msg';
  }
  await refreshData(false); // Odśwież dane z nowym filtrem profilu!
}

function fmtTime(sec){
  const h = Math.floor(sec/3600), m = Math.round((sec%3600)/60);
  return h>0 ? `${h}h ${m}m` : `${m}m`;
}
function fmtDateShort(d){
  const months = ['sty','lut','mar','kwi','maj','cze','lip','sie','wrz','paź','lis','gru'];
  const [y,mo,da] = d.split('-');
  return `${parseInt(da)} ${months[parseInt(mo)-1]}`;
}
function colorFor(name){
  if(name === ME_NAME) return '#ef476f';
  const idx = crew.filter(n=>n!==ME_NAME).indexOf(name);
  const pal = PALETTE.filter(c=>c!=='#ef476f');
  return pal[idx % pal.length];
}

function loadCrew(){
  try{
    const raw = localStorage.getItem(getCrewStorageKey());
    if(raw){
      const arr = JSON.parse(raw);
      if(Array.isArray(arr) && arr.length){ crew = arr.filter(n => DATA?.users?.[n]); return; }
    }
  }catch(e){}
  crew = [];
  if (ME_NAME && DATA?.users?.[ME_NAME]) {
    crew.push(ME_NAME);
  }
}

async function saveCrew(){
  try{ localStorage.setItem(getCrewStorageKey(), JSON.stringify(crew)); }
  catch(e){}
  
  if (!ME_NAME && (!crew || crew.length === 0)) {
    return;
  }
  
  try {
    localStorage.setItem('dashboard-profile-id', profileId);
    localStorage.setItem('dashboard-me', ME_NAME);
    await fetch(`/api/v1/crew?id=${encodeURIComponent(profileId || crewId)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        id: profileId || crewId,
        me: ME_NAME,
        members: crew
      })
    });
  } catch(err) {
    console.error('Nie udało się zapisać profilu na serwerze:', err);
  }
}

function renderChips(){
  const wrap = document.getElementById('chips');
  wrap.innerHTML = '';
  if(crew.length===0){
    wrap.innerHTML = '<span style="color:var(--muted);font-size:13px;">Ekipa jest pusta — dodaj kogoś poniżej.</span>';
    return;
  }
  crew.forEach(name=>{
    const u = DATA.users[name];
    const chip = document.createElement('div');
    chip.className = 'chip' + (name===ME_NAME ? ' me' : '');
    chip.innerHTML = `<span>${name}${name===ME_NAME?' <span class="tag-me">JA</span>':''}</span><span class="rankdot">#${u?u.rank:'?'}</span>`;
    const btn = document.createElement('button');
    btn.innerHTML = '×';
    btn.title = 'Usuń z ekipy';
    btn.onclick = async ()=>{
      crew = crew.filter(n=>n!==name);
      await saveCrew();
      await refreshData(false);
    };
    chip.appendChild(btn);
    wrap.appendChild(chip);
  });
}

let ALL_NAMES = null;

async function ensureNamesLoaded() {
  if (ALL_NAMES) return ALL_NAMES;
  try {
    const resp = await fetch(`/api/v1/challenge/${CHALLENGE_SLUG}/names`);
    if (resp.ok) {
      ALL_NAMES = await resp.json();
    }
  } catch (e) {
    console.error('Failed to load challenge names:', e);
  }
  return ALL_NAMES || [];
}

function setupCustomAutocomplete(inputId, suggestionsId, getSourceFn, onSelect) {
  const input = document.getElementById(inputId);
  const list = document.getElementById(suggestionsId);
  if (!input || !list) return;

  let activeIndex = -1;
  let currentItems = [];

  function closeSuggestions() {
    list.classList.remove('show');
    list.innerHTML = '';
    activeIndex = -1;
  }

  async function showSuggestions(query) {
    const source = await getSourceFn();
    const cleanQuery = query.toLowerCase().trim();
    if (!cleanQuery) {
      closeSuggestions();
      return;
    }

    currentItems = source.filter(item => {
      const str = typeof item === 'object' && item !== null ? (item.me || item.name || '') : item;
      return String(str).toLowerCase().includes(cleanQuery);
    }).slice(0, 6);

    if (currentItems.length === 0) {
      closeSuggestions();
      return;
    }

    list.innerHTML = currentItems.map((item, idx) => {
      const str = typeof item === 'object' && item !== null ? (item.me || item.name || '') : item;
      const cleanStr = String(str);
      const startIdx = cleanStr.toLowerCase().indexOf(cleanQuery);
      const endIdx = startIdx + cleanQuery.length;
      const highlighted = cleanStr.substring(0, startIdx) + 
                          `<span class="match">${cleanStr.substring(startIdx, endIdx)}</span>` + 
                          cleanStr.substring(endIdx);
      return `<div class="suggestion-item" data-index="${idx}">${highlighted}</div>`;
    }).join('');

    list.classList.add('show');
    activeIndex = -1;

    list.querySelectorAll('.suggestion-item').forEach(el => {
      el.onclick = () => {
        const val = currentItems[parseInt(el.dataset.index)];
        const str = typeof val === 'object' && val !== null ? (val.me || val.name || '') : val;
        input.value = String(str);
        closeSuggestions();
        if (onSelect) onSelect(val);
      };
    });
  }

  input.addEventListener('focus', () => {
    ensureNamesLoaded();
    if (input.value) showSuggestions(input.value);
  });

  input.addEventListener('input', () => {
    showSuggestions(input.value);
  });

  input.addEventListener('keydown', (e) => {
    const items = list.querySelectorAll('.suggestion-item');
    if (!list.classList.contains('show') || items.length === 0) return;

    if (e.key === 'ArrowDown') {
      e.preventDefault();
      activeIndex = (activeIndex + 1) % items.length;
      updateActiveItem(items);
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      activeIndex = (activeIndex - 1 + items.length) % items.length;
      updateActiveItem(items);
    } else if (e.key === 'Enter') {
      e.preventDefault();
      if (activeIndex >= 0 && activeIndex < currentItems.length) {
        const val = currentItems[activeIndex];
        const str = typeof val === 'object' && val !== null ? (val.me || val.name || '') : val;
        input.value = String(str);
        closeSuggestions();
        if (onSelect) onSelect(val);
      }
    } else if (e.key === 'Escape') {
      closeSuggestions();
    }
  });

  function updateActiveItem(items) {
    items.forEach((item, idx) => {
      item.classList.toggle('active', idx === activeIndex);
      if (idx === activeIndex) {
        item.scrollIntoView({ block: 'nearest' });
      }
    });
  }

  document.addEventListener('click', (e) => {
    if (!input.contains(e.target) && !list.contains(e.target)) {
      closeSuggestions();
    }
  });
}

function renderDatalist() {
  // Obsolete function kept for structure compatibility. Datalists are replaced with custom autocomplete.
}

function renderStats(){
  const grid = document.getElementById('statsGrid');
  if (!grid || !DATA) return;
  const top = DATA.topLeaders?.[0];
  const active = crew.filter(n=>DATA.users[n]);
  
  const totalCrewPoints = active.reduce((sum, name) => sum + (DATA.users[name]?.points || 0), 0);
  const avgCrewPoints = active.length ? totalCrewPoints / active.length : 0;
  const topCrew = active.length ? active.reduce((best, name) => (DATA.users[name]?.points || 0) > (DATA.users[best]?.points || 0) ? name : best, active[0]) : '—';
  
  const totalDistance = active.reduce((sum, name) => sum + (DATA.users[name]?.distance || 0), 0);
  const totalTime = active.reduce((sum, name) => sum + (DATA.users[name]?.time || 0), 0);
  const avgRank = active.length ? active.reduce((sum, name) => sum + (DATA.users[name]?.rank || 0), 0) / active.length : 0;
  const savedCO2 = totalDistance * 0.120; // 120g per km

  const cards = [
    {label:'Lider wyzwania', value: top?.name || '—', sub: `${(top?.points || 0).toFixed(1)} pkt`},
    {label:'Twoja ekipa', value: active.length ? `${active.length} osób` : '0 osób', sub: `${totalCrewPoints.toFixed(1)} pkt`},
    {label:'Śr. punktów ekipy', value: avgCrewPoints.toFixed(1), sub: 'na osobę w ekipie'},
    {label:'Lider ekipy', value: topCrew === '—' ? '—' : topCrew, sub: `${topCrew === '—' ? 0 : (DATA.users[topCrew]?.points || 0).toFixed(1)} pkt`},
    {label:'Oszczędność CO₂', value: `${savedCO2.toFixed(1)} kg`, sub: 'zielony transport'},
    {label:'Dystans ekipy', value: `${totalDistance.toFixed(1)} km`, sub: 'pokonana odległość'},
    {label:'Czas w ruchu', value: fmtTime(totalTime), sub: 'łącznie treningów'},
    {label:'Średnia pozycja', value: active.length ? `#${Math.round(avgRank)}` : '—', sub: 'w gen. wyzwania'}
  ];
  
  grid.innerHTML = cards.map(card => `
    <div class="stat-card">
      <div class="stat-label">${card.label}</div>
      <div class="stat-value" title="${card.value}">${card.value}</div>
      <div class="stat-sub">${card.sub}</div>
    </div>`).join('');
}

function renderBibs(){
  const grid = document.getElementById('bibGrid');
  grid.innerHTML = '';
  if(crew.length===0){
    grid.innerHTML = '<div class="empty">Brak zawodników w ekipie. Dodaj pierwszą osobę powyżej, żeby zobaczyć jej kartę startową.</div>';
    return;
  }
  crew.forEach(name=>{
    const u = DATA.users[name];
    if(!u) return;
    const c = colorFor(name);
    const card = document.createElement('div');
    card.className = 'bib';
    card.innerHTML = `
      <div class="accentbar" style="background:${c}"></div>
      <div class="bib-rank">MIEJSCE #${u.rank} / ${DATA.totalUsers}</div>
      <div class="bib-name">${name}${name===ME_NAME?'<span class="tag-me">JA</span>':''}</div>
      <div class="bib-points" style="color:${c}">${u.points.toFixed(0)}</div>
      <div class="bib-points-label">punktów w wyzwaniu</div>
      <div class="bib-stats">
        <div><b>${u.distance.toFixed(1)} km</b><span>Dystans</span></div>
        <div><b>${fmtTime(u.time)}</b><span>Czas</span></div>
        <div><b>${u.count}</b><span>Aktywności</span></div>
        <div><b>${u.elevation.toFixed(0)} m</b><span>Przewyższenie</span></div>
      </div>
      <svg class="spark" viewBox="0 0 240 34" preserveAspectRatio="none"></svg>
    `;
    grid.appendChild(card);
    drawSpark(card.querySelector('.spark'), DATA.allDates.map(d=>u.daily[d].points), c);
  });
}

function drawSpark(svg, values, color){
  const w=240, h=34, pad=3;
  const max = Math.max(...values, 1);
  const step = (w-2*pad)/(values.length-1 || 1);
  const pts = values.map((v,i)=>{
    const x = pad + i*step;
    const y = h - pad - (v/max)*(h-2*pad);
    return `${x},${y}`;
  });
  const line = document.createElementNS('http://www.w3.org/2000/svg','polyline');
  line.setAttribute('points', pts.join(' '));
  line.setAttribute('fill','none');
  line.setAttribute('stroke', color);
  line.setAttribute('stroke-width','2');
  line.setAttribute('stroke-linecap','round');
  line.setAttribute('stroke-linejoin','round');
  svg.appendChild(line);
  values.forEach((v,i)=>{
    const [x,y] = pts[i].split(',');
    const dot = document.createElementNS('http://www.w3.org/2000/svg','circle');
    dot.setAttribute('cx',x); dot.setAttribute('cy',y); dot.setAttribute('r', i===values.length-1?'3':'0');
    dot.setAttribute('fill', color);
    svg.appendChild(dot);
  });
}

function destroyChart(key){ if(charts[key]){ charts[key].destroy(); delete charts[key]; } }

function renderBarChart(){
  destroyChart('bar');
  const ctx = document.getElementById('barChart');
  const present = crew.filter(n=>DATA.users[n]).slice().sort((a,b)=>DATA.users[b].points-DATA.users[a].points);
  charts.bar = new Chart(ctx, {
    type:'bar',
    data:{
      labels: present,
      datasets:[{
        data: present.map(n=>DATA.users[n].points),
        backgroundColor: present.map(n=>colorFor(n)),
        borderRadius:6,
        maxBarThickness:46,
      }]
    },
    options:{
      responsive:true,
      maintainAspectRatio:false,
      indexAxis:'y',
      plugins:{legend:{display:false}, tooltip:{callbacks:{label:(c)=>` ${c.raw.toFixed(1)} pkt`}}},
      scales:{
        x:{grid:{color:'#1c3a54'}, ticks:{color:'#7f9bb4', font:{family:'JetBrains Mono'}}},
        y:{grid:{display:false}, ticks:{color:'#eef4f8', font:{family:'Inter', size:13}}}
      }
    }
  });
}

function renderCumulativeChart(){
  destroyChart('cumulative');
  const ctx = document.getElementById('cumulativeChart');
  const present = crew.filter(n=>DATA.users[n]);
  charts.cumulative = new Chart(ctx, {
    type:'line',
    data:{
      labels: DATA.allDates.map(fmtDateShort),
      datasets: present.map(name=>{
        let sum = 0;
        const cumulativeData = DATA.allDates.map(d=>{
          sum += DATA.users[name].daily[d].points;
          return sum;
        });
        return {
          label:name,
          data: cumulativeData,
          borderColor: colorFor(name),
          backgroundColor: colorFor(name)+'22',
          borderWidth: name===ME_NAME?3:2,
          tension:0.35,
          pointRadius:3,
          pointBackgroundColor: colorFor(name),
        };
      })
    },
    options:{
      responsive:true,
      maintainAspectRatio:false,
      plugins:{legend:{position:'bottom', labels:{color:'#eef4f8', font:{family:'Inter', size:12}, boxWidth:10, boxHeight:10}}},
      scales:{
        x:{grid:{color:'#1c3a54'}, ticks:{color:'#7f9bb4'}},
        y:{grid:{color:'#1c3a54'}, ticks:{color:'#7f9bb4'}, title:{display:true, text:'suma punktów', color:'#7f9bb4'}}
      }
    }
  });
}

function renderLineChart(){
  destroyChart('line');
  const ctx = document.getElementById('lineChart');
  const present = crew.filter(n=>DATA.users[n]);
  charts.line = new Chart(ctx, {
    type:'line',
    data:{
      labels: DATA.allDates.map(fmtDateShort),
      datasets: present.map(name=>({
        label:name,
        data: DATA.allDates.map(d=>DATA.users[name].daily[d].points),
        borderColor: colorFor(name),
        backgroundColor: colorFor(name)+'22',
        borderWidth: name===ME_NAME?3:2,
        tension:0.35,
        pointRadius:3,
        pointBackgroundColor: colorFor(name),
      }))
    },
    options:{
      responsive:true,
      maintainAspectRatio:false,
      plugins:{legend:{position:'bottom', labels:{color:'#eef4f8', font:{family:'Inter', size:12}, boxWidth:10, boxHeight:10}}},
      scales:{
        x:{grid:{color:'#1c3a54'}, ticks:{color:'#7f9bb4'}},
        y:{grid:{color:'#1c3a54'}, ticks:{color:'#7f9bb4'}, title:{display:true, text:'punkty / dzień', color:'#7f9bb4'}}
      }
    }
  });
}

function renderStackChart(){
  destroyChart('stack');
  const ctx = document.getElementById('stackChart');
  const present = crew.filter(n=>DATA.users[n]);
  const allTypes = Object.keys(TYPE_LABELS).filter(t=>present.some(n=>DATA.users[n].byType[t]));
  const canvasWrap = ctx.parentElement;
  canvasWrap.style.height = Math.max(120, present.length*54+60)+'px';
  charts.stack = new Chart(ctx, {
    type:'bar',
    data:{
      labels: present,
      datasets: allTypes.map(t=>({
        label: TYPE_LABELS[t],
        data: present.map(n=> (DATA.users[n].byType[t]?.points) || 0),
        backgroundColor: TYPE_COLORS[t],
        borderRadius:4,
      }))
    },
    options:{
      indexAxis:'y',
      responsive:true,
      maintainAspectRatio:false,
      plugins:{legend:{position:'bottom', labels:{color:'#eef4f8', font:{family:'Inter', size:11.5}, boxWidth:10, boxHeight:10}}},
      scales:{
        x:{stacked:true, grid:{color:'#1c3a54'}, ticks:{color:'#7f9bb4'}},
        y:{stacked:true, grid:{display:false}, ticks:{color:'#eef4f8', font:{size:13}}}
      }
    }
  });
}

function renderTop10(){
  const tbody = document.getElementById('top10Body');
  tbody.innerHTML = DATA.topLeaders.map(l=>`
    <tr class="${crew.includes(l.name)?'tracked':''}">
      <td class="rankcol">${l.rank}</td>
      <td>${l.name}${l.name===ME_NAME?' <span class="tag-me">JA</span>':''}</td>
      <td class="ptscol">${l.points.toFixed(1)}</td>
    </tr>`).join('');
}

function renderAll(){
  renderChips();
  renderStats();
  renderBibs();
  renderBarChart();
  renderCumulativeChart();
  renderLineChart();
  renderStackChart();
  renderTop10();
}

document.getElementById('addBtn').onclick = addAthlete;
document.getElementById('addInput').addEventListener('keydown', e=>{ if(e.key==='Enter'){ e.preventDefault(); addAthlete(); }});
document.getElementById('authPanel').addEventListener('submit', loginToStravit);
document.getElementById('createProfileBtn').onclick = createProfile;
document.getElementById('profileCollapseBtn')?.addEventListener('click', toggleProfileCard);
async function loadProfileById(id) {
  const profile = savedProfiles.find(p => p.id === id);
  if (!profile) return;

  try {
    const resp = await fetch(`/api/v1/crew?id=${encodeURIComponent(profile.id)}`);
    if (resp.ok) {
      const payload = await resp.json();
      if (payload && typeof payload === 'object') {
        applyProfile(payload, true); // Wczytaj z flagą odświeżania API
        return;
      }
    }
  } catch (e) {
    console.error('Błąd wczytywania profilu:', e);
  }
  applyProfile({ id: profile.id, me: profile.me, members: [] }, true);
}

document.getElementById('loadProfileBtn').onclick = async ()=>{
  const input = document.getElementById('profileInput');
  const value = (input?.value || '').trim();
  if (!value) return;
  const profile = savedProfiles.find(p => (p.me || '').trim().toLowerCase() === value.trim().toLowerCase());
  if (profile) {
    await loadProfileById(profile.id);
    input.value = ''; // Wyczyść pole wyszukiwania po udanym wczytaniu
    const profileMsg = document.getElementById('profileMsg');
    if (profileMsg) profileMsg.textContent = '';
  } else {
    const profileMsg = document.getElementById('profileMsg');
    if (profileMsg) {
      profileMsg.textContent = `Nie znaleziono profilu dla "${value}".`;
      profileMsg.className = 'add-msg err';
    }
  }
};
document.getElementById('profileInput')?.addEventListener('keydown', e=>{
  if (e.key === 'Enter') {
    e.preventDefault();
    document.getElementById('loadProfileBtn').click();
  }
});

async function addAthlete(){
  const input = document.getElementById('addInput');
  const msg = document.getElementById('addMsg');
  const val = input.value.trim();
  msg.className = 'add-msg';
  if(!val){ return; }
  
  const names = await ensureNamesLoaded();
  const match = names.find(n=>n.toLowerCase()===val.toLowerCase());
  if(!match){
    const close = names.filter(n=>n.toLowerCase().includes(val.toLowerCase())).slice(0,3);
    msg.className = 'add-msg err';
    msg.textContent = close.length ? `Nie znalazłem "${val}". Może chodzi o: ${close.join(', ')}?` : `Nie znalazłem zawodnika "${val}" w wyzwaniu.`;
    return;
  }
  if(crew.includes(match)){
    msg.className = 'add-msg err';
    msg.textContent = `${match} jest już w Twojej ekipie.`;
    return;
  }
  crew.push(match);
  input.value = '';
  msg.textContent = '';
  await saveCrew();
  await refreshData(false);
}

(async function init(){
  // Obsługa panelu bocznego (Drawer dla mobile, Collapse/Expand dla desktop)
  const toggleMobileBtn = document.getElementById('toggleSidebarBtnMobile');
  const toggleDesktopBtn = document.getElementById('toggleSidebarBtnDesktop');
  const closeSidebarBtn = document.getElementById('closeSidebarBtn');
  const sidebarBackdrop = document.getElementById('sidebarBackdrop');
  const gridLayout = document.getElementById('dashboardGridLayout');

  const openSidebarMobile = () => {
    document.body.classList.add('sidebar-open');
  };
  const closeSidebarMobile = () => {
    document.body.classList.remove('sidebar-open');
  };

  if (toggleMobileBtn) {
    toggleMobileBtn.onclick = openSidebarMobile;
  }
  if (closeSidebarBtn) {
    closeSidebarBtn.onclick = closeSidebarMobile;
  }
  if (sidebarBackdrop) {
    sidebarBackdrop.onclick = closeSidebarMobile;
  }

  // Obsługa zwijania panelu na desktopie
  if (toggleDesktopBtn && gridLayout) {
    const isCollapsed = localStorage.getItem('dashboard-sidebar-collapsed') === 'true';
    if (isCollapsed) {
      gridLayout.classList.add('sidebar-collapsed');
      toggleDesktopBtn.textContent = '▶';
    }
    toggleDesktopBtn.onclick = () => {
      const collapsedNow = gridLayout.classList.toggle('sidebar-collapsed');
      localStorage.setItem('dashboard-sidebar-collapsed', collapsedNow ? 'true' : 'false');
      toggleDesktopBtn.textContent = collapsedNow ? '▶' : '◀';
      
      // Zmień rozmiar wykresów po zakończeniu animacji
      setTimeout(() => {
        Object.values(charts).forEach(c => {
          if (c) c.resize();
        });
      }, 360);
    };
  }

  // Obsługa zakładek wykresów dla urządzeń mobilnych
  document.querySelectorAll('.chart-tab').forEach(tab => {
    tab.onclick = () => {
      document.querySelectorAll('.chart-tab').forEach(t => t.classList.remove('active'));
      tab.classList.add('active');
      const chartId = tab.dataset.chart;
      document.querySelectorAll('.chart-card-wrapper').forEach(card => {
        card.classList.toggle('active', card.dataset.chart === chartId);
      });
      // Wymuś odrysowanie wykresu w nowo pokazanym kontenerze
      const activeChart = charts[chartId];
      if (activeChart) {
        setTimeout(() => {
          activeChart.resize();
        }, 50);
      }
    };
  });

  // Podepnij przycisk odświeżania
  const btn = document.getElementById('refreshBtn');
  if (btn) btn.onclick = () => refreshData(true);

  // Wczytaj z lokalnego cache przeglądarki dla natychmiastowego startu
  try {
    const cachedData = localStorage.getItem('dashboard-cached-data');
    if (cachedData) {
      DATA = JSON.parse(cachedData);
      loadCrew();
      updateHeader();
      renderAll();
    }
  } catch(e) {
    console.warn('Failed to load initial cached data:', e);
  }

  // Sprawdź status autoryzacji serwera
  try {
    const statusResp = await fetch('/api/v1/auth/status');
    if (statusResp.ok) {
      const statusData = await statusResp.json();
      if (statusData.hasMasterCredentials) {
        const authMsg = document.getElementById('authMsg');
        if (authMsg) {
          authMsg.textContent = 'Autoryzacja serwera włączona (Master)';
          authMsg.style.color = 'var(--teal)';
        }
      }
    }
  } catch(e) {}

  // Inicjalizacja autouzupełniania dla pól wyszukiwania
  setupCustomAutocomplete('addInput', 'addSuggestions', ensureNamesLoaded);
  setupCustomAutocomplete('profileInput', 'profileSuggestions', () => savedProfiles, async (profile) => {
    if (profile && profile.id) {
      await loadProfileById(profile.id);
      document.getElementById('profileInput').value = '';
    }
  });
  setupCustomAutocomplete('meInput', 'meSuggestions', ensureNamesLoaded);

  // Załaduj dane (automatycznie pobierze profil w Promise.all)
  await refreshData(false);
})();

let modalChart = null;

function openChartModal(chartKey) {
  const modal = document.getElementById('chartModal');
  const modalCanvas = document.getElementById('modalChartCanvas');
  
  if (modalChart) {
    modalChart.destroy();
    modalChart = null;
  }
  
  const originalChart = charts[chartKey];
  if (!originalChart) return;
  
  let title = "Wykres";
  let subtitle = "";
  if (chartKey === 'cumulative') {
    title = "Postęp skumulowany";
    subtitle = "Łączna suma punktów w kolejnych dniach";
  } else if (chartKey === 'bar') {
    title = "Punkty — porównanie";
    subtitle = "Łączne punkty członków ekipy";
  } else if (chartKey === 'line') {
    title = "Dzienna forma";
    subtitle = "Punkty zdobyte każdego dnia wyzwania";
  } else if (chartKey === 'stack') {
    title = "Z czego te punkty";
    subtitle = "Rozkład punktów według typu treningu";
  }
  
  document.getElementById('modalChartTitle').textContent = title;
  document.getElementById('modalChartSubtitle').textContent = subtitle;
  
  const config = {
    type: originalChart.config.type,
    data: JSON.parse(JSON.stringify(originalChart.config.data)),
    options: JSON.parse(JSON.stringify(originalChart.config.options))
  };
  
  config.options.maintainAspectRatio = false;
  config.options.responsive = true;
  
  if (config.options.scales) {
    if (config.options.scales.x && config.options.scales.x.ticks) {
      config.options.scales.x.ticks.font = { size: 12, family: 'Inter' };
    }
    if (config.options.scales.y && config.options.scales.y.ticks) {
      config.options.scales.y.ticks.font = { size: 12, family: 'Inter' };
    }
  }
  if (config.options.plugins && config.options.plugins.legend && config.options.plugins.legend.labels) {
    config.options.plugins.legend.labels.font = { size: 13, family: 'Inter' };
  }
  
  if (originalChart.config.options.plugins && originalChart.config.options.plugins.tooltip && originalChart.config.options.plugins.tooltip.callbacks) {
    config.options.plugins.tooltip = config.options.plugins.tooltip || {};
    config.options.plugins.tooltip.callbacks = originalChart.config.options.plugins.tooltip.callbacks;
  }
  
  modalChart = new Chart(modalCanvas, config);
  modal.classList.add('show');
  
  const handleEsc = (e) => {
    if (e.key === 'Escape') {
      closeChartModal();
      document.removeEventListener('keydown', handleEsc);
    }
  };
  document.addEventListener('keydown', handleEsc);
}

function closeChartModal() {
  const modal = document.getElementById('chartModal');
  modal.classList.remove('show');
  if (modalChart) {
    modalChart.destroy();
    modalChart = null;
  }
}