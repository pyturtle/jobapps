// Relays page requests to your JobApps server (content scripts can't reach it directly) and
// fills the current tab when you press Alt+J or use "Fill this page" in the toolbar popup.
importScripts('config.js');

async function call(path, body, method) {
  const res = await fetch(JOBAPPS_SERVER + path, {
    method: method || (body ? 'POST' : 'GET'),
    headers: { 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `server said ${res.status}`);
  return data;
}

async function fillTab(tabId) {
  if (!tabId) return;
  const files = ['config.js', 'filler.js', 'content.js'];
  try {
    await chrome.scripting.executeScript({ target: { tabId, allFrames: true }, files });
  } catch (e) {
    // Some frames belong to sites we have no access to; the page and the job-board frames still work.
    try { await chrome.scripting.executeScript({ target: { tabId }, files }); } catch (e2) { /* content script may already be there */ }
  }
  try { await chrome.tabs.sendMessage(tabId, { type: 'fill' }); } catch (e) { /* no listener in this tab */ }
}

chrome.runtime.onMessage.addListener((msg, sender, reply) => {
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
