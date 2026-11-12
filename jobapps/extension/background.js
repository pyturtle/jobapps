// Relays page requests to your JobApps server (content scripts can't reach it directly) and
// fills the current tab when you press Alt+J or use "Fill this page" in the toolbar popup.
importScripts('config.js');

// Every request has a deadline: Chrome may stop this worker while it waits, and the page would wait forever.
// (Drafting, which takes longer, runs in the background on the server; see content.js.)
const TIMEOUT_MS = 25000;

async function call(path, body, method) {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), TIMEOUT_MS);
  try {
    const res = await fetch(JOBAPPS_SERVER + path, {
      method: method || (body ? 'POST' : 'GET'),
      headers: { 'Content-Type': 'application/json' },
      body: body ? JSON.stringify(body) : undefined,
      signal: ctl.signal,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || `server said ${res.status}`);
    return data;
  } catch (e) {
    if (e && e.name === 'AbortError') throw new Error(`no answer from your server in ${TIMEOUT_MS / 1000} seconds`);
    throw e;
  } finally {
    clearTimeout(timer);
  }
}

const FILES = ['config.js', 'filler.js', 'content.js'];

async function injectTab(tabId) {
  try {
    await chrome.scripting.executeScript({ target: { tabId, allFrames: true }, files: FILES });
  } catch (e) {
    // Some frames belong to sites we have no access to; the page and the job-board frames still work.
    try { await chrome.scripting.executeScript({ target: { tabId }, files: FILES }); } catch (e2) { /* content script may already be there */ }
  }
}

async function fillTab(tabId) {
  if (!tabId) return;
  await injectTab(tabId);
  try { await chrome.tabs.sendMessage(tabId, { type: 'fill' }); } catch (e) { /* no listener in this tab */ }
}

// Career sites you turned on from the popup ("Run JobApps on this site"). The manifest's job boards get the
// scripts by themselves; an application that continues on a company's own site only did after Alt+J, so its
// time went uncounted. A site you allow gets the same scripts, registered here, and loses them if you remove
// the site's access in chrome://extensions.
const BUILT_IN = new Set(chrome.runtime.getManifest().host_permissions || []);

async function syncSites() {
  const { origins = [] } = await chrome.permissions.getAll();
  const sites = origins.filter((o) => !BUILT_IN.has(o) && /^https:\/\/[^*/]+\/\*$/.test(o));
  await chrome.scripting.unregisterContentScripts({ ids: ['sites'] }).catch(() => {});
  if (sites.length) {
    await chrome.scripting.registerContentScripts([{ id: 'sites', matches: sites, js: FILES, allFrames: true, runAt: 'document_idle' }]);
  }
  return sites;
}

chrome.permissions.onAdded.addListener(async (added) => {
  await syncSites().catch(() => {});
  // The page you were on when you allowed it starts right away (the popup may already have closed).
  for (const origin of added.origins || []) {
    const tabs = await chrome.tabs.query({ url: origin }).catch(() => []);
    for (const t of tabs) await injectTab(t.id);
  }
});
chrome.permissions.onRemoved.addListener(() => { syncSites().catch(() => {}); });
chrome.runtime.onInstalled.addListener(() => { syncSites().catch(() => {}); });

// Which of today's jobs each tab is working on. An application often continues on another site (a career page
// hands you to its ATS, Workday shows your candidate home after you submit): pages there still belong to that job,
// for the timer and for "applied". Only a page of the same company inherits it, for 3 hours after the last use.
const TAB_JOB_MS = 3 * 60 * 60 * 1000;
// Words that don't tell one employer from another: parts of an address, sites that host many employers' applications
// (two companies on taleo.net are not the same company), and words many company names share.
const COMMON = new Set(['www', 'careers', 'career', 'jobs', 'job', 'apply', 'campus', 'com', 'ca', 'net', 'org', 'io', 'ai', 'co',
  'myworkdayjobs', 'myworkdaysite', 'icims', 'eightfold', 'oraclecloud', 'workable', 'greenhouse', 'lever', 'ashbyhq',
  'smartrecruiters', 'bamboohr', 'jobvite', 'rippling', 'boards', 'board', 'hcm', 'fa', 'ocs', 'en', 'us', 'inc', 'ltd',
  'taleo', 'successfactors', 'sapsf', 'adp', 'workforcenow', 'ultipro', 'ukg', 'dayforce', 'dayforcehcm', 'csod', 'avature',
  'phenompeople', 'recruitee', 'teamtailor', 'breezy', 'pinpointhq', 'applytojob', 'jazzhr', 'paylocity', 'paycomonline',
  'brassring', 'njoyn', 'linkedin', 'indeed', 'glassdoor', 'ziprecruiter', 'wellfound', 'simplify', 'joinhandshake',
  'symplicity', 'ripplematch', 'recruiting', 'recruit', 'talent', 'hire', 'hiring', 'candidate', 'candidates', 'portal',
  'login', 'auth', 'secure', 'account', 'accounts', 'app', 'apps', 'site', 'sites', 'pages', 'web', 'online', 'cloud',
  'corp', 'corporation', 'company', 'group', 'llc', 'limited', 'technologies', 'technology', 'tech', 'systems', 'solutions',
  'software', 'services', 'labs', 'lab', 'digital', 'data', 'global', 'international', 'canada', 'the', 'and']);
const tokens = (text) => (String(text || '').toLowerCase().match(/[a-z0-9]+/g) || [])
  .filter((t) => t.length >= 3 && !COMMON.has(t) && !/^wd\d+$/.test(t));

async function tabJob(tabId, host) {
  const key = `tab:${tabId}`;
  const rec = (await chrome.storage.session.get(key))[key];
  if (!rec || Date.now() - rec.at > TAB_JOB_MS) return null;
  const mine = new Set([...rec.hosts.flatMap(tokens), ...tokens(rec.company)]);
  return tokens(host).some((t) => mine.has(t)) ? { jobId: rec.jobId, age: Date.now() - rec.at } : null;
}

async function setTabJob(tabId, job, host) {
  const key = `tab:${tabId}`;
  const rec = (await chrome.storage.session.get(key))[key];
  const hosts = rec && rec.jobId === job.id ? [...new Set([...rec.hosts, host])].slice(-6) : [host];
  await chrome.storage.session.set({ [key]: { jobId: job.id, company: job.company, hosts, at: Date.now() } });
}

chrome.tabs.onRemoved.addListener((tabId) => { chrome.storage.session.remove(`tab:${tabId}`).catch(() => {}); });
chrome.tabs.onCreated.addListener(async (tab) => {  // a window the application opens (sign-in, ATS) is the same job
  if (!tab.openerTabId) return;
  const rec = (await chrome.storage.session.get(`tab:${tab.openerTabId}`))[`tab:${tab.openerTabId}`];
  if (rec) await chrome.storage.session.set({ [`tab:${tab.id}`]: rec });
});

chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  const tabId = sender && sender.tab && sender.tab.id;
  if (msg && msg.type === 'tab-job') {
    tabJob(tabId, msg.host).then((r) => reply(r || { jobId: null }), () => reply({ jobId: null }));
    return true;
  }
  if (msg && msg.type === 'tab-job-set') {
    if (tabId && msg.job && msg.job.id) setTabJob(tabId, msg.job, msg.host).catch(() => {});
    return false;
  }
  if (msg && msg.type === 'api') {
    call(msg.path, msg.body, msg.method).then(
      (data) => reply({ ok: true, data }),
      (err) => reply({ ok: false, error: `${(err && err.message) || err} (is your home server reachable over Tailscale?)` }),
    );
    return true; // async reply
  }
  if (msg && msg.type === 'fill-tab') {
    fillTab(msg.tabId).then(() => reply({ ok: true }));
    return true;
  }
  return false;
});

chrome.commands.onCommand.addListener(async (command) => {
  if (command !== 'fill') return;
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (tab) fillTab(tab.id);
});

// Updates without re-downloading: mac-setup.sh keeps this folder in sync with your server. When the
// manifest on disk is a newer build than the one running, reload so the new files take effect.
async function checkForUpdate() {
  try {
    const res = await fetch(chrome.runtime.getURL('manifest.json'), { cache: 'no-store' });
    const onDisk = await res.json();
    if (onDisk.version_name && onDisk.version_name !== chrome.runtime.getManifest().version_name) chrome.runtime.reload();
  } catch (e) { /* the folder is being synced right now; next check */ }
}
chrome.alarms.create('jobapps-update', { periodInMinutes: 5 });
chrome.alarms.onAlarm.addListener((alarm) => { if (alarm.name === 'jobapps-update') checkForUpdate(); });
chrome.runtime.onStartup.addListener(checkForUpdate);
chrome.runtime.onStartup.addListener(() => { syncSites().catch(() => {}); });
