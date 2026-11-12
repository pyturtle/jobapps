// Runs in each frame of an application page. Finds fields (filler.js), asks your JobApps server
// what goes in each one, and types it in. The server decides every value; this script only acts.
// It never clicks submit.
(() => {
  if (window.__jobappsContent) return;
  window.__jobappsContent = true;

  const ATTR = 'data-jobapps-id';
  // Dropdown options across widget libraries: ARIA listboxes, react-select, Greenhouse, Lever's
  // location suggestions, Google Places.
  const OPTION_SEL = '[role="option"], [id*="-option-"], .select__option, .dropdown-location, .pac-item';
  const PLACEHOLDER_OPTION = /^(no options|no results|no matches|loading|searching|type to search)/i;
  // A page that says the application went in. The same two patterns as jobapps/plan.py (a test keeps them identical).
  const SUBMITTED = /thank(s| you) for (applying|your application|submitting)|application (was |has been )?(successfully )?(submitted|received|sent|completed?)\b|we('ve| have) received your application|your application (is|was|has been) (in *[.!]|submitted\b|received\b)/gi;
  const NOT_YET = /\b(once|after|when|if|until|before|as soon as|whether)\b[^.!?\n]*(?![\s\S])/i;
  const inFrame = window.top !== window;
  const J = () => window.__jobapps;
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  let job = null;
  let force = false;
  let busy = false;
  let formUrl = '';               // the last application page with fields on it (kept in your record)
  const draftAsked = new Set();  // questions sent for drafting already (per field, so later wizard pages get theirs)
  const drafting = new Set();    // fields whose answers your server is drafting right now
  let draftError = '';
  const optionsWaited = new Set();  // dropdowns whose late options we waited for once
  let done = false;
  let note = '';
  let reopen = false;
  let needsRealClick = false;    // this page's dropdowns ignore scripted clicks (e.g. new Greenhouse forms)
  const handled = new Map();     // field id -> {field, action, state, typed}
  const userValues = new Map();  // field id -> latest value you entered
  const assisted = new Map();    // field id -> action, picked for you once you open the dropdown yourself

  class NeedsClick extends Error {}
  class Gone extends Error {}      // the page re-rendered and replaced the field; rescan and retry
  let retries = 0;

  // The extension updated itself while this page was open: this copy of the script is orphaned.
  const orphaned = (msg) => new Error(/context invalidated|receiving end does not exist/i.test(msg || '')
    ? 'JobApps just updated. Reload this page to use the new version.' : msg);

  function sendOnce(path, body, method) {
    return new Promise((resolve, reject) => {
      try {
        chrome.runtime.sendMessage({ type: 'api', path, body, method }, (r) => {
          if (chrome.runtime.lastError) return reject(orphaned(chrome.runtime.lastError.message));
          if (r && r.ok) resolve(r.data); else reject(new Error(r ? r.error : 'no response from the extension'));
        });
      } catch (e) {
        reject(orphaned(e.message));
      }
    });
  }

  // Chrome stops an extension's service worker when it has been busy or idle too long, and a request it was
  // relaying then ends with "message port closed". Asking again wakes a new one.
  async function send(path, body, method) {
    try {
      return await sendOnce(path, body, method);
    } catch (e) {
      if (!/message port closed/i.test((e && e.message) || '')) throw e;
      await sleep(400);
      return sendOnce(path, body, method);
    }
  }

  // The job this tab was working on before this page (see background.js): an application that continues on another
  // page or site stays with its job instead of becoming a page you found yourself.
  let tabJobId = null;
  let tabJobAge = Infinity;
  function askBackground(msg) {
    return new Promise((resolve) => {
      try { chrome.runtime.sendMessage(msg, (r) => resolve(chrome.runtime.lastError ? null : r)); } catch (e) { resolve(null); }
    });
  }
  async function loadTabJob() {
    const r = await askBackground({ type: 'tab-job', host: location.host });
    if (r && r.jobId) { tabJobId = r.jobId; tabJobAge = r.age ?? Infinity; }
  }
  function keepTabJob(j) {
    if (j && j.in_batch) askBackground({ type: 'tab-job-set', job: { id: j.id, company: j.company }, host: location.host });
  }

  const ctx = () => ({ url: location.href, frame_url: location.href, title: document.title, job_id: (job && job.id) || tabJobId });

  // ------------------------------------------------------------- typing

  // Type the way a keyboard does (execCommand insertText), so sites built on React and similar
  // frameworks register the change. Falls back to setting the value directly.
  function typeInto(el, text) {
    el.focus();
    if (typeof el.select === 'function') el.select();
    let ok = false;
    // execCommand types into whatever has focus; a dropdown that just closed can grab it back, so
    // only use it when this field really is focused (otherwise set this field directly below).
    if (document.activeElement === el) {
      try { ok = document.execCommand(text ? 'insertText' : 'delete', false, text); } catch (e) { ok = false; }
    }
    if (!ok || el.value !== text) {
      const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, text);
      el.dispatchEvent(new InputEvent('input', { bubbles: true, data: text, inputType: 'insertText' }));
    }
    // Some widgets (Lever's location search, older jQuery forms) only react to key events.
    const key = text ? text.slice(-1) : 'Backspace';
    el.dispatchEvent(new KeyboardEvent('keydown', { key, bubbles: true }));
    el.dispatchEvent(new KeyboardEvent('keyup', { key, bubbles: true }));
  }

  const expanded = (el) => el.getAttribute('aria-expanded') === 'true';
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const shownEl = (o) => (o.checkVisibility ? o.checkVisibility() : !!o.offsetParent);
  const NOT_AN_OPTION = '[data-automation-id="selectedItemList"], [data-jobapps-panel]';  // Workday's chosen chips are role=option too
  const optionText = (o) => {
    const l = o.matches('[data-automation-label]') ? o : o.querySelector('[data-automation-label]');
    return clean(l ? l.getAttribute('data-automation-label') : o.innerText);
  };

  // The events a real mouse click produces, in order (as testing-library/user-event does): pointer
  // events with pointerType "mouse" for Radix / Headless UI, mousedown for react-select / MUI, then click.
  function press(el) {
    const r = el.getBoundingClientRect();
    const at = { bubbles: true, cancelable: true, composed: true, view: window, button: 0,
                 clientX: r.left + r.width / 2, clientY: r.top + r.height / 2 };
    const ptr = { ...at, pointerId: 1, pointerType: 'mouse', isPrimary: true };
    el.dispatchEvent(new PointerEvent('pointerover', ptr));
    el.dispatchEvent(new MouseEvent('mouseover', at));
    el.dispatchEvent(new PointerEvent('pointerdown', { ...ptr, buttons: 1 }));
    el.dispatchEvent(new MouseEvent('mousedown', { ...at, buttons: 1 }));
    el.dispatchEvent(new PointerEvent('pointerup', ptr));
    el.dispatchEvent(new MouseEvent('mouseup', at));
    el.click();
  }

  function visibleOptions() {
    return [...document.querySelectorAll(OPTION_SEL)]
      .filter((o) => !o.closest(NOT_AN_OPTION) && shownEl(o))
      .filter((o) => { const t = clean(o.innerText); return t && !PLACEHOLDER_OPTION.test(t) && !/^select one$/i.test(t); });
  }

  async function pickFrom(opts, candidates, pickFirst, exact) {
    if (!opts.length) return null;
    const texts = opts.map(optionText);
    const r = await send('/api/ext/choose', { candidates, options: texts, pick_first: pickFirst, exact: !!exact });
    if (r.index < 0) return null;
    const o = opts[r.index];
    o.scrollIntoView({ block: 'nearest' });
    press(o);
    return texts[r.index];
  }

  const pickOption = (candidates, pickFirst) => pickFrom(visibleOptions(), candidates, pickFirst);

  function openMenu(el) {
    el.focus();
    const control = el.closest('[class*="control" i]') || el.parentElement;
    for (const target of [control, el]) {
      if (target) target.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true, button: 0 }));
    }
    const arrow = { key: 'ArrowDown', code: 'ArrowDown', keyCode: 40, bubbles: true, cancelable: true };
    el.dispatchEvent(new KeyboardEvent('keydown', arrow));
    // Greenhouse's job-board forms keep the menu's open state themselves and open it on keyup only.
    el.dispatchEvent(new KeyboardEvent('keyup', arrow));
  }

  // ------------------------------------------------------------- Workday-style widgets

  const WD = /(^|\.)myworkday(jobs|site)\.com$/.test(location.hostname);

  function setValue(el, text) {
    const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, text);
    el.dispatchEvent(new InputEvent('input', { bubbles: true, data: text, inputType: 'insertText' }));
  }

  function pressKey(el, key, keyCode) {
    for (const type of ['keydown', 'keypress', 'keyup']) {
      el.dispatchEvent(new KeyboardEvent(type, { key, code: key, keyCode, which: keyCode, bubbles: true, cancelable: true }));
    }
  }

  // Click somewhere neutral to close a menu. (Escape would roll back a Workday dropdown's choice.)
  function clickAway() {
    const spot = document.querySelector('#mainContent, main, [role="main"]') || document.body;
    for (const type of ['mousedown', 'mouseup', 'click']) spot.dispatchEvent(new MouseEvent(type, { bubbles: true, button: 0 }));
  }

  // Options of the menu a control just opened: the one it points at (aria-controls), else the newest
  // open popup (Workday renders menus at the end of <body> and may leave closed ones behind).
  function menuOptions(control) {
    const roots = [];
    const ref = control && (control.getAttribute('aria-controls') || control.getAttribute('aria-owns'));
    if (ref && document.getElementById(ref)) roots.push(document.getElementById(ref));
    const pops = [...document.querySelectorAll('[data-automation-activepopup="true"], [data-automation-id="activeListContainer"], [visibility="opened"]')];
    if (pops.length) roots.push(pops[pops.length - 1]);
    roots.push(document);
    for (const root of roots) {
      const leaves = [...root.querySelectorAll('[data-automation-id="promptLeafNode"]')];
      const opts = (leaves.length ? leaves : [...root.querySelectorAll('[role="option"]')])
        .filter((o) => !o.closest(NOT_AN_OPTION) && shownEl(o))
        .filter((o) => { const t = optionText(o); return t && !/^select one$/i.test(t) && !PLACEHOLDER_OPTION.test(t); });
      if (opts.length) return opts;
    }
    return [];
  }

  async function waitFor(test, ms, step = 150) {
    for (let waited = 0; waited < ms; waited += step) {
      const v = test();
      if (v) return v;
      await sleep(step);
    }
    return test();
  }

  // Long lists only render the options in view: scroll through until the one we want shows up.
  async function findInLongList(control, candidates, pickFirst) {
    const first = menuOptions(control)[0];
    const list = first && (first.closest('[role="listbox"], .ReactVirtualized__Grid, ul') || first.parentElement);
    for (let i = 0; list && i < 30; i++) {
      const top = list.scrollTop;
      list.scrollTop += Math.max(60, list.clientHeight - 20);
      await sleep(90);
      if (list.scrollTop === top) break;
      const got = await pickFrom(menuOptions(control), candidates, pickFirst);
      if (got) return got;
    }
    return null;
  }

  // A button that opens a listbox: Workday, Radix, Headless UI, MUI.
  async function chooseDropdown(btn, candidates, pickFirst) {
    btn.scrollIntoView({ block: 'center' });
    const before = clean(btn.innerText);
    press(btn);
    let opts = await waitFor(() => { const o = menuOptions(btn); return o.length ? o : null; }, 2500);
    if (!opts) {  // some only open from the keyboard
      btn.focus();
      pressKey(btn, 'ArrowDown', 40);
      opts = await waitFor(() => { const o = menuOptions(btn); return o.length ? o : null; }, 1500);
    }
    if (!opts) throw new Error('its list did not open');
    await sleep(200);  // lists can arrive in pieces
    let got = await pickFrom(menuOptions(btn), candidates, pickFirst);
    if (!got) got = await findInLongList(btn, candidates, pickFirst);
    if (!got) { clickAway(); throw new Error('none of its options matched'); }
    await waitFor(() => clean(btn.innerText) !== before || !expanded(btn), 2000);
    if (expanded(btn)) clickAway();
    return got;
  }

  // Workday's search box that collects picks as chips (skills, field of study, how you heard).
  async function choosePrompt(input, items, exact, clear, pickFirst) {
    const box = input.closest('[data-automation-id="multiSelectContainer"]') || input.parentElement;
    const chipEls = () => [...box.querySelectorAll('[data-automation-id="selectedItem"]')];
    const lastChip = () => { const c = chipEls().pop(); return c ? optionText(c) : null; };
    input.scrollIntoView({ block: 'center' });
    if (clear) {
      for (const c of chipEls()) {
        const x = c.querySelector('[data-automation-id="DELETE_charm"]') || c;
        press(x);
        await sleep(250);
      }
      await waitFor(() => !chipEls().length, 2000);
    }
    const picked = [];
    const missed = [];
    for (const cands of items) {
      const n0 = chipEls().length;
      let got = null;
      const query = cands.find((c) => !SEMANTIC.test(c));
      if (query) {  // typing alone shows nothing: search runs on Enter
        input.focus();
        setValue(input, query.slice(0, 60));
        pressKey(input, 'Enter', 13);
        await waitFor(() => chipEls().length > n0 || menuOptions(input).length
          || /no (matches|results)/i.test(clean((document.querySelector('[data-automation-id="promptTitle"]') || {}).innerText)), 3000);
        if (chipEls().length > n0) {
          got = lastChip();  // a single match is chosen by Enter itself: keep it only if it's right
          const ok = await send('/api/ext/choose', { candidates: cands, options: [got], exact: !!exact });
          if (ok.index < 0) { press(chipEls().pop().querySelector('[data-automation-id="DELETE_charm"]') || chipEls().pop()); got = null; await sleep(300); }
        } else {
          got = await pickFrom(menuOptions(input), cands, false, exact);
        }
      }
      if (!got && !exact) {  // browse the list instead: top level, then into the matching category
        setValue(input, '');
        press(input);
        const top = await waitFor(() => { const o = menuOptions(input); return o.length ? o : null; }, 2500);
        if (top) {
          const shown = top.map(optionText).join('|');
          got = await pickFrom(top, cands, pickFirst);
          await sleep(500);
          if (got && chipEls().length === n0) {  // it was a category: its children replaced the list
            const inner = await waitFor(() => { const o = menuOptions(input); return o.length && o.map(optionText).join('|') !== shown ? o : null; }, 2000);
            got = inner ? await pickFrom(inner, cands, true) : null;
          }
        }
      }
      if (got) await waitFor(() => chipEls().length > n0, 2000);
      if (got && chipEls().length > n0) picked.push(got); else missed.push(cands[0]);
      setValue(input, '');
    }
    pressKey(input, 'Escape', 27);
    clickAway();
    if (!picked.length) throw new Error(`couldn't find ${missed.slice(0, 3).join(', ')} in its list`);
    return { picked, missed };
  }

  // A date split into spin boxes. Year first: on an empty Workday date, a month typed first is taken as the year.
  async function fillDate(wrap, parts) {
    const box = (p) => wrap.querySelector(`[data-automation-id="dateSection${p[0].toUpperCase()}${p.slice(1)}-input"]`);
    const want = (p) => (p === 'year' ? String(parts[p]) : String(parts[p]).padStart(2, '0'));
    const now = (p) => { const b = box(p); const v = (b && (b.value || b.getAttribute('aria-valuenow'))) || ''; return /^\d+$/.test(v) ? Number(v) : null; };
    const order = ['year', 'month', 'day'].filter((p) => p in parts);
    wrap.scrollIntoView({ block: 'center' });
    for (const p of order) {
      const b = box(p);
      if (!b) throw new Error(`no ${p} box`);
      b.focus();
      setValue(b, want(p));
      b.dispatchEvent(new Event('change', { bubbles: true }));
      await sleep(60);
    }
    if (document.activeElement && wrap.contains(document.activeElement)) document.activeElement.blur();
    await sleep(200);
    if (order.every((p) => now(p) === Number(parts[p]))) return order.map((p) => want(p)).join('/');
    // Didn't stick: type it digit by digit, the way a keyboard would.
    for (const p of order) {
      const b = box(p);
      b.focus();
      for (const ch of want(p)) {
        b.dispatchEvent(new KeyboardEvent('keydown', { key: ch, code: `Digit${ch}`, keyCode: 48 + Number(ch), bubbles: true, cancelable: true }));
        try { document.execCommand('insertText', false, ch); } catch (e) { /* not editable that way */ }
        b.dispatchEvent(new KeyboardEvent('keyup', { key: ch, code: `Digit${ch}`, keyCode: 48 + Number(ch), bubbles: true }));
      }
      await sleep(60);
    }
    if (document.activeElement && wrap.contains(document.activeElement)) document.activeElement.blur();
    await sleep(200);
    if (!order.every((p) => now(p) === Number(parts[p]))) throw new Error('the date did not stick');
    return order.map((p) => want(p)).join('/');
  }

  // Workday's repeated blocks: press "Add" until there's one block per job / school / link you have.
  const SECTION_KIND = { work: /work|employment|experience/i, education: /education/i, website: /website/i };
  const sectionsDone = new Set();
  let extraBlocks = [];

  async function addSections(wants) {
    if (!WD) return 0;
    let added = 0;
    extraBlocks = [];
    for (const { kind, want } of wants || []) {
      const sec = [...document.querySelectorAll('[role="group"][aria-labelledby$="-section"]')]
        .find((s) => SECTION_KIND[kind] && SECTION_KIND[kind].test(clean((document.getElementById(s.getAttribute('aria-labelledby')) || {}).textContent)));
      if (!sec) continue;
      const blocks = () => [...sec.querySelectorAll('[role="group"][aria-labelledby$="-panel"]')];
      if (blocks().length > want) {
        extraBlocks.push(...blocks().slice(want).map((b) => clean((document.getElementById(b.getAttribute('aria-labelledby')) || {}).textContent)));
      }
      if (sectionsDone.has(kind)) continue;  // once per page: going Back must not add duplicates
      sectionsDone.add(kind);
      while (blocks().length < want && added < 10) {
        const add = [...sec.querySelectorAll('button')].filter((b) => !b.closest('[aria-labelledby$="-panel"]')
          && (b.getAttribute('data-automation-id') === 'add-button' || /^add( another)?\b/i.test(clean(b.innerText) || b.getAttribute('aria-label') || ''))).pop();
        if (!add) break;
        const n = blocks().length;
        press(add);
        if (!(await waitFor(() => blocks().length > n, 5000))) break;
        added += 1;
        await sleep(250);
      }
    }
    return added;
  }

  // Answers like "decline" or "yes" are matched against the options, never typed as text.
  const SEMANTIC = /^(decline|yes|no|true|false)$/i;

  async function chooseFromList(el, candidates, pickFirst) {
    if (needsRealClick) throw new NeedsClick();
    openMenu(el);
    await sleep(350);
    if (!visibleOptions().length) {
      // Many dropdowns open their full list once you type: type a space and clear it again.
      typeInto(el, ' ');
      await sleep(150);
      typeInto(el, '');
      await sleep(350);
    }
    let got = await pickOption(candidates, pickFirst);
    for (const query of got ? [] : candidates.filter((c) => !SEMANTIC.test(c)).slice(0, 3)) {
      typeInto(el, query.slice(0, 60));
      // An async menu (schools, cities) shows itself as expanded while it searches; give it time.
      for (let i = 0; i < 5 && !visibleOptions().length && !expanded(el); i++) await sleep(300);
      if (!visibleOptions().length && !expanded(el)) {
        // Nothing reacted to scripted input at all: this site only accepts real clicks.
        typeInto(el, '');
        needsRealClick = true;
        throw new NeedsClick();
      }
      for (let i = 0; i < 12 && !visibleOptions().length; i++) await sleep(300);
      await sleep(200);
      got = await pickOption(candidates, pickFirst);
      if (got) break;
    }
    if (got) return got;
    typeInto(el, '');
    el.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', code: 'Escape', keyCode: 27, bubbles: true }));
    if (!candidates.some((c) => !SEMANTIC.test(c)) && !visibleOptions().length) {
      needsRealClick = true;  // couldn't even open it to compare the options
      throw new NeedsClick();
    }
    throw new Error('none of its options matched');
  }

  // You open a dropdown that only accepts real clicks; JobApps picks the planned option.
  async function assistPick(id) {
    const a = assisted.get(id);
    const el = document.querySelector(`[${ATTR}="${id}"]`);
    if (!a || !el) return;
    for (let i = 0; i < 10 && !visibleOptions().length; i++) await sleep(120);
    const query = a.candidates.find((c) => !SEMANTIC.test(c));
    if (!visibleOptions().length && query) {
      typeInto(el, query.split(',')[0].slice(0, 60));
      for (let i = 0; i < 14 && !visibleOptions().length; i++) await sleep(250);
    }
    const got = await pickOption(a.candidates, a.pick_first).catch(() => null);
    if (!got) return;
    assisted.delete(id);
    const x = handled.get(id);
    if (x) { x.state = 'filled'; x.typed = got; x.action.note = ''; }
    J().mark(id, 'filled', '');
    render();
  }

  function assistTarget(e) {
    const box = e.target && e.target.closest && e.target.closest('[data-jobapps-box], [data-jobapps-id]');
    if (!box) return null;
    const id = box.getAttribute('data-jobapps-box') || box.getAttribute('data-jobapps-id');
    return assisted.has(id) ? id : null;
  }
  document.addEventListener('pointerdown', (e) => { const id = assistTarget(e); if (id) assistPick(id); }, true);
  document.addEventListener('keydown', (e) => {
    if (['ArrowDown', 'Enter', ' '].includes(e.key)) { const id = assistTarget(e); if (id) assistPick(id); }
  }, true);

  async function attach(el, key) {
    const f = await send(`/api/ext/file/${key}`, null, 'GET');
    const bytes = Uint8Array.from(atob(f.b64), (c) => c.charCodeAt(0));
    const dt = new DataTransfer();
    dt.items.add(new File([bytes], f.name, { type: f.type }));
    el.files = dt.files;
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
    return f.name;
  }

  // Wait until a suggestion list stops changing (searches answer out of order).
  async function settledOptions(maxMs) {
    let last = -1;
    for (let waited = 0; waited < maxMs; waited += 250) {
      const n = visibleOptions().length;
      if (n && n === last) return n;
      last = n;
      await sleep(250);
    }
    return visibleOptions().length;
  }

  async function fillText(el, text) {
    // Workday's text boxes are plain (its "location" in Work Experience is free text, not a place search).
    const looksLikeSearch = !WD && (/location|city|autocomplete|search/i.test(`${el.className} ${el.name} ${el.id}`)
      || el.getAttribute('aria-autocomplete') || el.getAttribute('list'));
    // A place search finds "Toronto" but not "Toronto, Ontario, Canada"; type the short form directly so
    // two searches can't race each other.
    const first = text.split(',')[0].trim();
    typeInto(el, looksLikeSearch && text.includes(',') ? first : text);
    await sleep(400);
    if (looksLikeSearch || visibleOptions().length) {
      for (let attempt = 0; attempt < 3; attempt++) {
        if (!(await settledOptions(looksLikeSearch ? 3000 : 600))) break;
        // An autocomplete (e.g. Lever's location) offered suggestions: pick the matching one,
        // otherwise the site clears what was typed when the field loses focus.
        const got = await pickOption([text, first], false);
        if (got) return got;
        await sleep(300);
      }
    }
    el.dispatchEvent(new Event('change', { bubbles: true }));
    el.blur();
    await sleep(150);
    if (!el.value) throw new Error('the site cleared it - pick from its list');
    return el.value;
  }

  async function perform(a) {
    const el = document.querySelector(`[${ATTR}="${a.id}"]`);
    if (!el) throw new Gone();
    switch (a.op) {
      case 'text': return fillText(el, a.text);
      case 'select':
        el.value = a.value;
        el.dispatchEvent(new Event('input', { bubbles: true }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
        return a.shown;
      case 'click':
        for (const i of a.indexes) {
          const m = document.querySelector(`[${ATTR}="${a.id}"][data-jobapps-opt="${i}"]`);
          if (!m) throw new Error('option disappeared');
          if (m.tagName === 'BUTTON' || !m.checked) m.click();
        }
        return a.shown.length === 1 ? a.shown[0] : a.shown;
      case 'check': {
        const on = a.on !== false;
        const isOn = () => (el.hasAttribute('aria-checked') ? el.getAttribute('aria-checked') === 'true' : el.checked);
        if (isOn() !== on) el.click();
        await waitFor(() => isOn() === on, 1000);  // Workday updates aria-checked a moment later
        return on;
      }
      case 'dropdown': return chooseDropdown(el, a.candidates, a.pick_first);
      case 'multiselect': {
        const r = await choosePrompt(el, a.items, a.exact, a.clear, a.pick_first);
        if (r.missed.length) a.info = `${a.label.replace(/^type to add /i, '')}: not in this site's list, so left out: ${r.missed.join(', ')}`;
        return r.picked.length === 1 ? r.picked[0] : r.picked;
      }
      case 'dateparts': return fillDate(el, a.parts);
      case 'combobox': {
        const got = await chooseFromList(el, a.candidates, a.pick_first);
        el.blur();  // let the dropdown close fully before the next field takes focus
        await sleep(120);
        return got;
      }
      case 'file': return attach(el, a.file);
      default: return null;
    }
  }

  // ------------------------------------------------------------- the timer
  // Each 30 s you're working on this application (page visible, and you typed, clicked or scrolled in the
  // last 2 minutes, or JobApps is filling) counts toward its time. A tab left idle doesn't.

  let lastInput = Date.now();
  let seconds = 0;
  for (const type of ['keydown', 'pointerdown', 'input', 'wheel', 'scroll']) {
    document.addEventListener(type, () => { lastInput = Date.now(); }, { capture: true, passive: true });
  }
  async function tick() {
    if (!job || done || !(force || job.in_batch) || document.visibilityState !== 'visible') return;
    if (!busy && Date.now() - lastInput > 120000) return;
    try {
      const r = await send('/api/ext/activity', ctx());
      if (r.seconds !== seconds) { seconds = r.seconds; if (handled.size) render(); }
    } catch (e) { /* the server is unreachable for now; the next tick tries again */ }
  }
  const tickTimer = setInterval(tick, 15000);  // twice per 30 s slice, so no active slice is missed
  const clock = () => (seconds >= 60 ? `${Math.round(seconds / 60)} min on this application` : '');

  // ------------------------------------------------------------- the panel

  function render() {
    if (!J() || (!handled.size && !note)) return;
    const items = [...handled.values()];
    const todo = items.filter((x) => x.state === 'missing' || x.state === 'manual')
      .map((x) => ({ id: x.action.id, label: x.action.label, required: x.state === 'missing', note: x.action.note }))
      .sort((a, b) => b.required - a.required);
    J().panel({
      title: job ? job.company : 'JobApps',
      subtitle: job ? job.title : '',
      filled: items.filter((x) => x.state === 'filled').length,
      drafts: items.filter((x) => x.state === 'draft').length,
      todo,
      drafted: items.filter((x) => x.state === 'draft').map((x) => ({ id: x.action.id, label: x.action.label })),
      note: [note, drafting.size ? `Drafting ${drafting.size} answer${drafting.size > 1 ? 's' : ''} from your resume (about a minute). Fill in the rest meanwhile.` : '',
        draftError].filter(Boolean).join(' '),
      timer: clock(),
      buttons: done ? [] : [{ id: 'applied', label: 'I submitted it', primary: true }, { id: 'rescan', label: 'Fill again' }],
      inFrame,
      done,
      reopen,
    });
    reopen = false;
  }

  // ------------------------------------------------------------- learning

  const same = (a, b) => JSON.stringify([].concat(a).map((x) => String(x).trim().toLowerCase()).sort())
    === JSON.stringify([].concat(b).map((x) => String(x).trim().toLowerCase()).sort());
  const hasValue = (v) => (Array.isArray(v) ? v.length > 0 : typeof v === 'boolean' ? v : String(v ?? '').trim() !== '');

  // Some pages reuse a box for a different question (Oracle re-renders its address form in place: the box that asked
  // Postal Code now asks City). What you type there answers the question it asks now, never the one JobApps filled.
  const qnorm = (s) => String(s || '').toLowerCase().replace(/[✱*]+|\((required|optional)\)/g, ' ').replace(/\s+/g, ' ').trim();
  const reused = (x, now) => !!(x && x.field && now && (qnorm(x.field.label) !== qnorm(now.label) || x.field.type !== now.type));
  // A dropdown planned before its options arrived, which has them now.
  const grewOptions = (x, now) => !!(x && x.field && now && x.field.type === 'select' && !(x.field.options || []).length
    && (now.options || []).length && x.state !== 'filled');
  const scanNow = () => new Map((J() ? J().scan() : []).map((f) => [f.id, f]));
  function forget(id) { handled.delete(id); assisted.delete(id); userValues.delete(id); }

  function learnable() {
    const entries = [];
    const edited = [];
    const now = scanNow();
    for (const [id, value] of userValues) {
      const x = handled.get(id);
      if (!x || !hasValue(value) || reused(x, now.get(id))) continue;
      const f = x.field;
      if (x.action.state === 'draft') {
        if (String(value).trim() !== String(x.typed || '').trim()) edited.push({ label: f.label, answer: String(value) });
        continue;
      }
      if (f.type === 'file' || f.type === 'textarea' || !f.label) continue;
      if (f.section) continue;  // "From" in Work Experience 2 or "Name" under References means nothing on its own
      if (x.typed != null && same(value, x.typed)) continue;
      if (String(value).length > 200) continue;
      entries.push({ label: f.label, answer: value, type: f.type });
    }
    return { entries, edited };
  }

  async function stash() {
    if (!job) return;
    await chrome.storage.local.set({ [`jobapps:${job.id}`]: learnable() });
  }

  async function finish(status) {
    if (done || !job) return;
    done = true;
    const key = `jobapps:${job.id}`;
    const stored = (await chrome.storage.local.get(key))[key] || { entries: [], edited: [] };
    const now = learnable();
    const merge = (a, b) => Object.values(Object.fromEntries([...a, ...b].map((e) => [e.label, e])));
    try {
      const r = await send('/api/ext/learn', { ...ctx(), entries: merge(stored.entries, now.entries), edited_drafts: merge(stored.edited, now.edited) });
      await send('/api/ext/status', { ...ctx(), status, form_url: formUrl || location.href });
      await chrome.storage.local.remove(key);
      unwatchPage();
      clearInterval(tickTimer);
      drafting.clear();
      note = `Marked as ${status} on your dashboard.${r.saved ? ` Remembered ${r.saved} answer${r.saved > 1 ? 's' : ''} you typed for next time.` : ''}`;
    } catch (e) {
      note = `Couldn't reach your JobApps server: ${e.message}`;
      done = false;
    }
    render();
  }

  // You answered a question JobApps couldn't: remember it right away (not only when you submit), so the
  // next form that asks the same thing fills itself.
  const rememberTimers = new Map();
  function rememberable(x, value) {
    const f = x.field || {};
    return hasValue(value) && f.label && !['file', 'textarea'].includes(f.type) && String(value).length <= 200
      && !f.section && x.action.rule !== 'draft';
  }
  function remember(id, value) {
    clearTimeout(rememberTimers.get(id));
    rememberTimers.set(id, setTimeout(async () => {
      const x = handled.get(id);
      if (!x || !rememberable(x, value)) return;
      if (reused(x, scanNow().get(id))) { forget(id); run(); return; }  // a new question in that box: plan it instead
      try {
        const r = await send('/api/ext/learn', { ...ctx(), entries: [{ label: x.field.label, answer: value, type: x.field.type, rule: x.action.rule }] });
        const where = (r.where || [])[0] || '';
        x.state = 'filled';
        x.typed = value;
        if (where === 'not stored') {  // a legal or consent question, or one your profile already answers
          x.action.note = 'answered by you';
        } else {
          x.action.note = where.startsWith('profile') ? 'saved to your profile' : 'remembered for next time';
          note = `Remembered “${x.field.label.slice(0, 60)}” for next time.`;
        }
        J().mark(id, 'filled', x.action.note);
        render();
      } catch (e) { /* kept locally and sent when you press "I submitted it" */ }
    }, 1200));
  }

  window.__jobappsRecord = (p) => {
    const id = String(p.id);
    userValues.set(id, p.value);
    clearTimeout(window.__jobappsStash);
    window.__jobappsStash = setTimeout(stash, 1500);
    const x = handled.get(id);
    if (x && (x.state === 'missing' || x.state === 'manual') && rememberable(x, p.value)) remember(id, p.value);
  };
  // The panel's buttons (filler.js calls this from the extension's own world; the page can't).
  function onAction(id) {
    if (id === 'applied') finish('applied');
    if (id === 'rescan') { fillAgain(); run(); }
    if (id === 'dismiss') { dismissed = true; J().closePanel(); unwatchPage(); }
    // Closed on a page you found yourself: stop watching it. Today's jobs keep the timer, the next page of the
    // application and "applied" going (the toolbar button brings the panel back).
    if (id === 'close' && !force && !(job && job.in_batch)) unwatchPage();
  }
  if (J()) J().onAction = onAction;

  // "Fill again" or Alt+J: fill this page, and ask again for drafts that didn't come.
  function fillAgain() {
    force = true;
    draftError = '';
    for (const id of [...draftAsked]) if (!drafting.has(id)) draftAsked.delete(id);
    watchPage();
  }

  // ------------------------------------------------------------- drafting
  // Your server drafts in the background (it often takes longer than an extension may wait for one reply) and
  // this page asks every few seconds how it is going. The rest of the page is filled meanwhile.

  let polling = false;
  async function startDrafts(ids, fields) {
    ids.forEach((id) => { draftAsked.add(id); drafting.add(id); });
    let r;
    try {
      r = await send('/api/ext/draft', { ...ctx(), fields: fields.filter((f) => ids.includes(f.id)),
        page_text: document.body.innerText.slice(0, 40000) });
    } catch (e) {
      ids.forEach((id) => drafting.delete(id));  // "Fill again" asks again
      draftError = `Drafting didn't work this time: ${e.message}`;
      return;
    }
    pollDrafts(r.job_id || (job && job.id));
  }

  function runSoon() {
    if (busy) { setTimeout(runSoon, 500); return; }
    run();
  }

  async function pollDrafts(jobId) {
    if (polling || !jobId) return;
    polling = true;
    const until = Date.now() + 6 * 60 * 1000;
    try {
      while (drafting.size && !done) {
        await sleep(3000);
        let r = null;
        try { r = await send(`/api/ext/draft/${encodeURIComponent(jobId)}`, null, 'GET'); } catch (e) { /* try again */ }
        if (r && r.state === 'running') {
          if (Date.now() < until) continue;
          draftError = 'Drafting is taking longer than usual. Press Fill again in a minute to fetch the answers.';
        } else if (!r) {
          if (Date.now() < until) continue;
          draftError = 'Lost touch with your server while it was drafting. Press Fill again.';
        } else if (r.state === 'error') {
          draftError = `Drafting didn't work this time: ${r.error}`;
        } else if (r.state === 'idle') {  // the server restarted and lost the request
          draftError = 'Drafting stopped because your server restarted. Press Fill again.';
        }
        // Plan these fields again: the answers are in now (a box you typed in meanwhile keeps your text).
        for (const id of drafting) { handled.delete(id); assisted.delete(id); }
        drafting.clear();
        render();
        runSoon();
        return;
      }
    } finally {
      polling = false;
    }
  }

  // ------------------------------------------------------------- main loop

  let again = false;  // the page changed while a fill was running: look once more when it's done
  async function run() {
    if (done || !J()) return;
    if (busy) { again = true; return; }
    busy = true;
    again = false;
    try {
      for (const id of [...handled.keys()]) {  // forget fields the page has since removed or re-rendered
        if (!document.querySelector(`[${ATTR}="${id}"]`)) { handled.delete(id); assisted.delete(id); }
      }
      let scanned = J().scan();
      // A dropdown whose options are still loading (some Province or Country lists arrive after the form): give it
      // a moment, so it isn't planned as a question JobApps has no answer for.
      const empty = scanned.filter((f) => f.type === 'select' && !f.options.length && !optionsWaited.has(f.id) && !handled.has(f.id));
      if (empty.length) {
        empty.forEach((f) => optionsWaited.add(f.id));
        const loaded = () => empty.every((f) => {
          const el = document.querySelector(`[${ATTR}="${f.id}"]`);
          // the same test as filler.js's scan: a placeholder ("Select...") isn't an option
          return !el || [...(el.options || [])].some((o) => o.value || (clean(o.text) && !/^(select|choose|please|--)/i.test(clean(o.text))));
        });
        await waitFor(loaded, 2000, 200);
        scanned = J().scan();
      }
      for (const f of scanned) {
        const x = handled.get(f.id);
        if (reused(x, f) || grewOptions(x, f)) forget(f.id);  // same box with a new question or new options: plan it afresh
      }
      const fields = scanned.filter((f) => !handled.has(f.id));
      if (fields.length && job && (force || job.in_batch)) formUrl = location.href;  // the form you filled, not the thank-you page
      if (pageUrl !== location.href) { pageUrl = location.href; announcedFor = ''; offeredFields = -1; }  // single-page apps change pages in place
      if (!fields.length) {
        if (!announcedFor && !inFrame) { announcedFor = pageUrl; await announce(); }
        if (reopen) render();
        return;
      }
      if (!force && job && !job.in_batch && fields.length === offeredFields) return;  // nothing new to offer
      let plan = await send('/api/ext/plan', { ...ctx(), fields });
      const first = !job;
      job = plan.job;
      keepTabJob(job);
      seconds = Math.max(seconds, job.seconds || 0);
      if (first) tick();  // start the clock now rather than in 30 s
      if (!force && !job.in_batch) { offer(plan, fields); return; }  // only today's jobs fill by themselves
      if (await addSections(plan.sections)) {  // new Work Experience / Education / Websites blocks: scan them first
        setTimeout(() => { busy = false; run(); }, 700);
        return;
      }
      const toDraft = plan.needs_draft.filter((id) => !draftAsked.has(id));
      if (toDraft.length && plan.can_draft) await startDrafts(toDraft, fields);
      // Fields that change the form go first (Country re-renders the address; "I currently work here"
      // removes the end date); uploads go last.
      const weight = (a) => (a.op === 'file' ? 3 : /^country/i.test(a.label) ? 0 : a.op === 'check' ? 1 : 2);
      const ordered = [...plan.actions].sort((a, b) => weight(a) - weight(b));
      const working = ordered.filter((a) => a.op).length;
      let step = 0;
      let lastPaint = 0;
      let vanished = 0;
      for (const a of ordered) {
        if (a.op) {
          step += 1;
          if (Date.now() - lastPaint > 500) {  // show progress while dropdowns take their time
            note = `Filling ${step} of ${working}…`;
            render();
            lastPaint = Date.now();
          }
        }
        const field = fields.find((f) => f.id === a.id);
        let state = a.state;
        let typed = null;
        if (!a.op && drafting.has(a.id)) {  // its answer is being drafted: filled in when it arrives
          state = 'drafting';
          a.note = 'being drafted from your resume';
        }
        if (a.op) {
          try {
            typed = await perform(a);
          } catch (e) {
            if (e instanceof Gone) { vanished += 1; continue; }  // picked up again by the rescan below
            state = a.required ? 'missing' : 'manual';
            if (e instanceof NeedsClick) {
              assisted.set(a.id, a);
              const want = (a.candidates || [])[0] || '';
              a.note = SEMANTIC.test(want) && want.toLowerCase() === 'decline'
                ? 'Click it and JobApps picks “decline to answer”'
                : `Click it and JobApps picks “${want || 'the answer'}”`;
            } else {
              a.note = `couldn't fill: ${e.message}`;
            }
          }
        }
        handled.set(a.id, { field, action: a, state, typed });
        J().mark(a.id, state === 'drafting' ? 'draft' : state, a.note);
      }
      if (note.startsWith('Filling ')) note = '';
      const infos = [...handled.values()].map((x) => x.action.info).filter(Boolean);
      if (extraBlocks.length) {
        const many = extraBlocks.length > 1;
        infos.push(`${extraBlocks.join(', ')} ${many ? "aren't" : "isn't"} in your profile (probably read from your resume by the site). Delete ${many ? 'them' : 'it'} if wrong.`);
      }
      if (infos.length) note = infos.join(' ');
      J().watch();
      render();
      if (vanished && retries < 3) {
        retries += 1;
        setTimeout(() => { busy = false; run(); }, 900);  // the page re-rendered mid-fill: fill the new copies
      }
    } catch (e) {
      note = e.message;
      if (force) {
        J().panel({ title: 'JobApps', subtitle: 'Couldn’t fill this page', filled: 0, drafts: 0, todo: [], note, inFrame, reopen: true });
      }
    } finally {
      busy = false;
      if (again && !done) { again = false; setTimeout(run, 300); }
    }
  }

  // A job posting with no form yet (e.g. Workday before you press Apply): say it's on today's list.
  let announcedFor = '';
  let pageUrl = '';
  async function announce() {
    const p = await send('/api/ext/plan', { ...ctx(), fields: [] }).catch(() => null);
    if (!p) return;
    job = p.job;
    keepTabJob(job);
    if (job.in_batch && !handled.size && !dismissed) {
      J().panel({ title: job.company, subtitle: job.title, compact: true, inFrame,
        note: 'On today’s list. Press Apply: JobApps fills each page of the application as it appears.' });
    }
  }

  // An application form for a job that isn't on today's list: offer to fill it rather than doing it unasked.
  let offeredFields = -1;
  let dismissed = false;
  function offer(plan, fields) {
    offeredFields = fields.length;
    const can = plan.actions.filter((a) => a.op).length;
    if (can < 3 || dismissed) return;  // too few fields we know: probably not an application form
    // Big platforms (ADP, Oracle Cloud) have other forms too: only offer on what looks like a job application.
    const jobish = /job|career|apply|recruit|candidate|position|opening|vacanc|requisition|talent/i.test(`${location.href} ${document.title}`);
    if (!jobish && !fields.some((f) => f.type === 'file' && /resume|cv/i.test(f.label))) return;
    J().panel({
      title: plan.job.known ? plan.job.company : 'Job application',
      subtitle: plan.job.known ? plan.job.title : document.title.slice(0, 90),
      compact: true, inFrame,
      note: `${plan.job.known ? 'Not on today’s list. ' : ''}JobApps can fill ${can} fields on this page.`,
      buttons: [{ id: 'rescan', label: 'Fill this page', primary: true }, { id: 'dismiss', label: 'Not now' }],
    });
  }

  // "Your application is incomplete" doesn't say so, and neither does a sentence about later ("Once your
  // application has been submitted, we will...").
  function saysSubmitted(text) {
    for (const m of text.matchAll(SUBMITTED)) {
      if (!NOT_YET.test(text.slice(Math.max(0, m.index - 80), m.index))) return true;
    }
    return false;
  }

  function submittedNow() {
    if (!J() || J().count() > 2) return false;  // a form is still on the page (and reading all its text is slow)
    return saysSubmitted((document.body && document.body.innerText.slice(0, 20000)) || '');
  }

  // A "submitted" page counts for one of today's jobs, and, when only this tab ties it to the job (Workday's
  // candidate home after you submit), only right after you were on its application: candidate home also lists
  // applications you sent days ago.
  const SETTLE_MS = 15 * 60 * 1000;
  const settles = (j) => j && j.in_batch && (j.id !== tabJobId || tabJobAge < SETTLE_MS || force);

  async function start() {
    await loadTabJob();
    // Wait for single-page apps to render their form (field count stable for a moment).
    let last = -1;
    for (let i = 0; i < 20; i++) {
      const n = J() ? J().scan().length : 0;
      if (n && n === last) break;
      last = n;
      await sleep(700);
    }
    if (submittedNow()) {
      // e.g. Lever's /thanks page: settle up for the application this tab just submitted
      const plan = await send('/api/ext/plan', { ...ctx(), fields: [] }).catch(() => null);
      if (plan && settles(plan.job)) { job = plan.job; finish('applied'); }
      return;
    }
    watchPage();  // before the first fill: a page that changes during it (late options, a new question) is looked at again
    await run();
  }

  // Watching the page for new fields (the next page of a wizard, a question that appears) and for the
  // confirmation page. A scan waits for the page to be quiet for a moment, runs at most every few seconds on a
  // page that never stops changing, and stops for good once the job is marked or you dismiss the offer.
  const QUIET_MS = 1200;     // a quiet moment after the last change
  const MAX_WAIT_MS = 5000;  // a page that keeps changing still gets a scan this often
  const MIN_GAP_MS = 2500;   // never sooner than this after the last scan
  let observer = null;
  let quietTimer = null;
  let firstChange = 0;
  let lastScan = 0;
  let changedWhileHidden = false;
  const OURS = '[data-jobapps-panel], [data-jobapps-rail]';
  const ours = (n) => n.nodeType === 1 && n.matches(OURS);
  const onlyOurs = (m) => (m.target.closest && m.target.closest(OURS))
    || ((m.addedNodes.length || m.removedNodes.length) && [...m.addedNodes, ...m.removedNodes].every(ours));

  function watchPage() {
    if (observer || done) return;
    observer = new MutationObserver((mutations) => {
      if (J() && J().ensurePanel) J().ensurePanel();  // sites that re-render the whole page remove it
      if (!mutations.every(onlyOurs)) schedule();
    });
    observer.observe(document.documentElement, { childList: true, subtree: true });
  }

  function unwatchPage() {
    if (observer) observer.disconnect();
    clearTimeout(quietTimer);
    observer = null;
    quietTimer = null;
    firstChange = 0;
  }

  function schedule() {
    const now = Date.now();
    if (!firstChange) firstChange = now;
    clearTimeout(quietTimer);
    const wait = Math.max(Math.min(QUIET_MS, MAX_WAIT_MS - (now - firstChange)), MIN_GAP_MS - (now - lastScan), 0);
    quietTimer = setTimeout(onQuiet, wait);
  }

  function onQuiet() {
    quietTimer = null;
    firstChange = 0;
    if (done || !observer) return;
    lastScan = Date.now();
    // not for a page you found yourself unless you asked JobApps to fill it (it read candidate homes as new applications)
    if ((force ? job : settles(job)) && submittedNow()) { finish('applied'); return; }
    if (document.visibilityState === 'hidden') { changedWhileHidden = true; return; }  // scanned when you come back
    if (force || (job ? job.in_batch || !dismissed : !inFrame)) run();  // offers re-check as forms appear
  }
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible' && changedWhileHidden && observer) { changedWhileHidden = false; schedule(); }
  });

  chrome.runtime.onMessage.addListener((msg) => {
    if (msg && msg.type === 'fill') { fillAgain(); reopen = true; run(); }
  });

  start();
})();
