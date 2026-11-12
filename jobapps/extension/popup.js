// Toolbar popup: today's applications from your JobApps server, and a button to fill the current page.
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const VERDICT = { strong: 'Strong', good: 'Good', stretch: 'Stretch', 'long shot': 'Long shot' };

async function state() {
  const res = await fetch(`${JOBAPPS_SERVER}/api/state`);
  if (!res.ok) throw new Error(`the server answered ${res.status}`);
  return res.json();
}

function fitChip(pick) {
  if (!pick) return '';
  const fit = pick.screen && pick.screen.fit;
  const cls = pick.verdict === 'long shot' ? 'stretch' : pick.verdict;
  const text = fit != null ? `${fit} fit` : VERDICT[pick.verdict] || '';
  return text ? `<span class="fit ${esc(cls)}">${esc(text)}</span>` : '';
}

function render(s, error) {
  const app = document.getElementById('app');
  const head = `<header><img src="icons/icon-48.png" alt=""><h1>JobApps</h1>
    <span class="conn ${error ? 'off' : ''}"><i></i>${error ? 'Not connected' : 'Connected'}</span></header>`;
  const mod = /Mac/i.test(navigator.platform || navigator.userAgent) ? '⌥ Option' : 'Alt';
  const fill = `<div class="cta"><button class="btn" id="fill">Fill this page</button>
    <p class="hint">or press <kbd>${mod}</kbd> + <kbd>J</kbd> on any application</p></div>`;
  const foot = `<footer><a href="${esc(JOBAPPS_SERVER)}/" target="_blank" rel="noopener">Open dashboard</a></footer>`;
  if (error) {
    app.innerHTML = `${head}<p class="problem">Can't reach your JobApps server (${esc(error)}). Check that Tailscale is on, then reopen this.</p>${fill}${foot}`;
  } else {
    const jobs = s.jobs || [];
    const date = new Date(`${s.date}T12:00:00`).toLocaleDateString(undefined, { weekday: 'long', month: 'short', day: 'numeric' });
    const list = jobs.length
      ? `<ol>${jobs.map((j) => `<li class="${j.status !== 'pending' ? 'done' : ''}"><button class="job" data-url="${esc(j.url)}" title="Open the application">
          <span class="n">${j.status === 'applied' ? '✓' : j.position}</span>
          <span><span class="co">${esc(j.company)}</span><span class="ti">${esc(j.title)}</span></span>${fitChip(j.pick)}</button></li>`).join('')}</ol>`
      : '<p class="muted pad">No jobs picked yet today. They arrive each morning.</p>';
    // Before the morning run, the server sends the jobs you haven't finished from earlier days.
    const day = s.preview
      ? `<div class="day"><p>${jobs.length ? 'Not finished from earlier days. New picks arrive this morning.' : esc(date)}</p></div>`
      : `<div class="day"><p>${esc(date)} &nbsp; <b>${s.done}</b> of <b>${jobs.length}</b> done</p>
      <div class="bar">${jobs.map((j) => `<span class="${esc(j.status)}"></span>`).join('')}</div></div>`;
    app.innerHTML = `${head}${day}${list}${fill}${foot}`;
  }
  offerSite();
  document.getElementById('fill').addEventListener('click', async () => {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    await chrome.runtime.sendMessage({ type: 'fill-tab', tabId: tab && tab.id });
    window.close();
  });
  document.querySelectorAll('.job').forEach((b) => b.addEventListener('click', () => {
    chrome.tabs.create({ url: b.dataset.url });
    window.close();
  }));
}

// An application that continues on a company's own career site: offer to run there by itself, as on the job
// boards (fills each page and counts your time). Chrome asks you to allow that one site.
async function offerSite() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  let url = null;
  try { url = tab && tab.url ? new URL(tab.url) : null; } catch (e) { url = null; }
  if (!url || url.protocol !== 'https:') return;
  const origins = [`${url.origin}/*`];
  if (await chrome.permissions.contains({ origins }).catch(() => true)) return;
  const box = document.createElement('div');
  box.className = 'site';
  box.innerHTML = `<button class="btn ghost" id="site">Run JobApps on ${esc(url.hostname)}</button>
    <p class="hint">Fills and times applications on this site by itself, as on the job boards.</p>`;
  document.querySelector('.cta').after(box);
  box.querySelector('button').addEventListener('click', async () => {
    // background.js starts it on this tab once Chrome says yes (this popup may close while Chrome asks).
    if (await chrome.permissions.request({ origins }).catch(() => false)) window.close();
  });
}

state().then((s) => render(s, null), (e) => render(null, e.message));

// A newer build is on disk (kept in sync by mac-setup.sh) but this one is still running: offer the reload
// in case the automatic one hasn't happened yet.
fetch(chrome.runtime.getURL('manifest.json'), { cache: 'no-store' }).then((r) => r.json()).then((disk) => {
  const running = chrome.runtime.getManifest().version_name;
  if (!disk.version_name || disk.version_name === running) return;
  const bar = document.createElement('div');
  bar.className = 'update';
  bar.innerHTML = '<span>An update is ready.</span><button class="link">Reload JobApps</button>';
  bar.querySelector('button').addEventListener('click', () => chrome.runtime.reload());
  document.body.prepend(bar);
}).catch(() => {});
