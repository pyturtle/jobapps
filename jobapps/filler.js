// Injected into every frame by filler.py. Finds form fields and their labels, outlines them by
// status, and reports values you change back to Python. It never clicks submit.
(() => {
  if (window.__jobapps) return;
  const ATTR = 'data-jobapps-id';
  const OPT = 'data-jobapps-opt';
  const OPT_LABEL = 'data-jobapps-label';
  const BOX = 'data-jobapps-box';
  const CONTROLS = 'input, textarea, select';
  const SKIP_INPUT = new Set(['hidden', 'submit', 'button', 'image', 'reset', 'password', 'search']);
  const EXCLUDE_TEXT = 'select, textarea, button, script, style, noscript, [role="listbox"], [role="option"], [data-jobapps-panel]';
  const LABELISH = 'legend, label, .application-label, [class*="label" i], [class*="question" i], [class*="title" i], h1, h2, h3, h4, h5, h6';
  const COLORS = { filled: '#16a34a', draft: '#2563eb', manual: '#d97706', missing: '#dc2626' };
  // Widgets that aren't plain inputs. Dropdown buttons: Workday, Radix, Headless UI, MUI. Search boxes
  // that collect picks as chips: Workday's "prompt" (skills, school, field of study, how you heard).
  const DROPDOWN = 'button[aria-haspopup="listbox"], button[role="combobox"], [role="combobox"][aria-haspopup="listbox"]:not(input), [role="button"][aria-haspopup="listbox"]';
  const PROMPT = 'input[data-uxi-widget-type="selectinput"], [data-automation-id="multiselectInputContainer"] input';
  const DATE_WRAP = '[data-automation-id="dateInputWrapper"]';
  const KIND = 'data-jobapps-kind';
  const PLACEHOLDER_TEXT = /^(select one|select|choose( one)?|please select|--+.*|none selected)$/i;
  // Instructions, not questions ("Select an option", "Choose...", "Type to search"): never a field's label.
  const NOT_A_LABEL = /^(please )?(select|choose|pick|enter|type)( (an?|one|your|here|to search))?( (option|answer|value|item|response|one)s?)?( from( the)? list)?\W*$|^(--+.*|none selected|search)$/i;
  const named = (...texts) => texts.map(clean).find((t) => t && !NOT_A_LABEL.test(t)) || '';

  // The text just before a field (its own ancestors' earlier siblings) when nothing labels it properly.
  function nearbyText(el) {
    for (let n = el, d = 0; n && d < 4; n = n.parentElement, d++) {
      for (let sib = n.previousElementSibling, k = 0; sib && k < 3; sib = sib.previousElementSibling, k++) {
        if (sib.matches(CONTROLS) || sib.querySelector(CONTROLS)) return '';  // reached the previous field
        const t = textOf(sib);
        if (t && t.length <= 250 && !NOT_A_LABEL.test(t)) return t;
      }
    }
    return '';
  }
  let seq = 0;
  let watching = false;

  const clean = s => (s || '').replace(/[\s ]+/g, ' ').trim();
  const shown = el => {
    if (!el || !el.isConnected) return false;
    if (el.checkVisibility) return el.checkVisibility({ checkVisibilityCSS: true });
    return !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  };

  function isCandidate(el) {
    if (el.disabled || el.closest('[data-jobapps-panel]')) return false;
    const t = (el.type || '').toLowerCase();
    if (el.tagName === 'INPUT') {
      if (el.closest(DATE_WRAP)) return false;  // a box of a split date; the date is one field
      const prev = el.previousElementSibling;
      if (prev && prev.matches(DROPDOWN)) return false;  // Workday's hidden mirror of a dropdown's value
      if (SKIP_INPUT.has(t)) return false;
      if (t === 'file') return shown(el) || shown(el.parentElement);
      // Hidden backing inputs (e.g. behind Yes/No buttons) are tabIndex -1; real styled radios are not.
      if (t === 'radio' || t === 'checkbox') return shown(el) || (el.tabIndex >= 0 && shown(el.closest('label') || el.parentElement));
      // Validation helpers rendered by select widgets: invisible, unfocusable, aria-hidden.
      if (el.getAttribute('aria-hidden') === 'true' || (el.tabIndex < 0 && parseFloat(getComputedStyle(el).opacity) < 0.05)) return false;
    }
    return shown(el);
  }

  const GENERIC_LABEL = /^(attach|upload( file)?|browse|choose( a)? files?|select( a)? files?|drop.*|enter manually|dropbox|google drive|click to upload|or)$/i;

  function textOf(node, skip = []) {
    if (!node) return '';
    const parts = [];
    const walker = document.createTreeWalker(node, NodeFilter.SHOW_TEXT, {
      acceptNode(t) {
        const p = t.parentElement;
        if (!p || p.closest(EXCLUDE_TEXT)) return NodeFilter.FILTER_REJECT;
        for (const s of skip) if (s && s.contains(p)) return NodeFilter.FILTER_REJECT;
        // Hidden status text ("Analyzing resume... Success!") is not part of the question.
        if (p.checkVisibility && !p.checkVisibility({ checkVisibilityCSS: true })) return NodeFilter.FILTER_REJECT;
        return NodeFilter.FILTER_ACCEPT;
      },
    });
    while (walker.nextNode()) parts.push(walker.currentNode.nodeValue);
    return clean(parts.join(' '));
  }

  function labelsFor(el) {
    const out = [];
    if (el.id) out.push(...document.querySelectorAll(`label[for="${CSS.escape(el.id)}"]`));
    const wrap = el.closest('label');
    if (wrap) out.push(wrap);
    return out;
  }

  function explicitLabel(el) {
    const by = el.getAttribute('aria-labelledby');
    if (by) {
      const t = clean(by.split(/\s+/).map(id => textOf(document.getElementById(id))).join(' '));
      if (t) return t;
    }
    for (const l of labelsFor(el)) {
      // A label that wraps the whole question block (Lever) also holds button text such as
      // "ATTACH RESUME/CV"; prefer its own caption element when it has one.
      const caption = l.contains(el) ? [...l.querySelectorAll('.application-label, [class*="label" i]')].find((c) => !c.contains(el)) : null;
      const t = textOf(caption || l);
      if (t && !GENERIC_LABEL.test(t)) return t;
    }
    const aria = clean(el.getAttribute('aria-label'));
    return GENERIC_LABEL.test(aria) ? '' : aria;
  }

  // Highest ancestor that contains no other field: the field's own "question block".
  function container(members) {
    let node = members[0].parentElement;
    let best = node;
    for (let d = 0; node && d < 8; d++, node = node.parentElement) {
      if (node.tagName === 'FORM' || node === document.body || node === document.documentElement) break;
      const others = [...node.querySelectorAll(CONTROLS)].filter(c => !members.includes(c) && isCandidate(c));
      if (others.length) break;
      best = node;
    }
    return best;
  }

  function groupLabel(cont, members) {
    // A fieldset/group names these members only if it holds nothing else (not a whole "Work Experience 1" block).
    const onlyThese = g => members.every(m => g.contains(m))
      && ![...g.querySelectorAll(CONTROLS)].some(c => !members.includes(c) && isCandidate(c));
    const fs = members[0].closest('fieldset');
    if (fs && onlyThese(fs)) {
      const t = textOf(fs.querySelector('legend'));
      if (t) return t;
    }
    const grp = members[0].closest('[role="radiogroup"], [role="group"]');
    if (grp && onlyThese(grp)) {
      const t = explicitLabel(grp);
      if (t) return t;
    }
    if (!cont) return '';
    const optionLabels = members.flatMap(labelsFor);
    for (const cand of cont.querySelectorAll(LABELISH)) {
      if (members.some(m => cand.contains(m))) continue;
      if (optionLabels.some(l => l === cand || l.contains(cand))) continue;
      const t = textOf(cand);
      if (t) return t;
    }
    return textOf(cont, [...optionLabels, ...members]);
  }

  function isRequired(members, label, cont) {
    if (members.some(m => m.required || m.getAttribute('aria-required') === 'true')) return true;
    if (members.some(m => /\brequired\s*$/i.test(m.getAttribute('aria-label') || ''))) return true;  // Workday buttons
    if (/[*✱]/.test(label)) return true;
    return !!(cont && cont.querySelector('[class*="required" i]'));
  }

  // Blocks that ask about someone else: their name, email and phone are never yours.
  const OTHER_PERSON = /\b(references?|referees?|emergency contacts?|next of kin|guardians?|spouse|dependents?|beneficiar(y|ies))\b/i;

  // Heading of the block a field sits in when the heading changes what its labels mean: a repeated block
  // ("Work Experience 2", "Education 1") or one about another person ("References"); '' if none.
  function sectionOf(el) {
    for (let n = el.parentElement, d = 0; n && d < 18; n = n.parentElement, d++) {
      if (!n.matches('[role="group"], fieldset, section')) continue;
      let head = '';
      const by = n.getAttribute('aria-labelledby');
      if (by) head = clean(by.split(/\s+/).map((x) => { const e = document.getElementById(x); return e ? e.textContent : ''; }).join(' '));
      if (!head) {
        const hd = n.querySelector(':scope > legend, :scope > h3, :scope > h4, :scope > h5, :scope > div > h4, :scope > div > h5');
        head = hd ? clean(hd.textContent) : '';
      }
      if (head.length < 60 && /\S\s+\d{1,2}$/.test(head)) return head;
      if (head.length < 40 && !head.includes('?') && OTHER_PERSON.test(head)) return head;
    }
    return '';
  }

  // Heading of the titled group around a field (Workday: "Resume/CV" around its upload box).
  function groupHeading(el) {
    const g = el.closest('[role="group"][aria-labelledby]');
    const hd = g && document.getElementById(g.getAttribute('aria-labelledby').split(/\s+/)[0]);
    return hd ? clean(hd.textContent) : '';
  }

  function chips(input) {
    const box = input.closest('[data-automation-id="multiSelectContainer"]') || input.parentElement;
    return [...box.querySelectorAll('[data-automation-id="selectedItem"]')].map((c) => {
      const p = c.querySelector('[data-automation-label]');
      return clean(p ? p.getAttribute('data-automation-label') : c.innerText);
    }).filter(Boolean);
  }

  const DATE_PARTS = ['month', 'day', 'year'];
  const datePart = (wrap, p) => wrap.querySelector(`[data-automation-id="dateSection${p[0].toUpperCase()}${p.slice(1)}-input"]`);

  function dateValue(wrap) {
    const parts = DATE_PARTS.filter((p) => datePart(wrap, p));
    const vals = parts.map((p) => {
      const inp = datePart(wrap, p);
      const v = clean(inp.value || inp.getAttribute('aria-valuenow') || '');
      return /^\d+$/.test(v) ? (p === 'year' ? v : v.padStart(2, '0')) : '';
    });
    return vals.every(Boolean) ? vals.join('/') : '';
  }

  function valueOf(id) {
    const els = [...document.querySelectorAll(`[${ATTR}="${id}"]`)];
    if (!els.length) return null;
    const el = els[0];
    const kind = el.getAttribute(KIND);
    if (kind === 'dropdown') { const t = clean(el.innerText); return PLACEHOLDER_TEXT.test(t) ? '' : t; }
    if (kind === 'multiselect') return chips(el);
    if (kind === 'date') return dateValue(el);
    if (el.hasAttribute(OPT)) {
      return els.filter(m => m.tagName === 'BUTTON'
        ? (m.getAttribute('aria-pressed') === 'true' || /(active|selected|checked)/i.test(m.className))
        : m.checked).map(m => m.getAttribute(OPT_LABEL) || '');
    }
    if (el.tagName === 'SELECT') return el.value ? clean(el.selectedOptions[0] && el.selectedOptions[0].text) : '';
    if (el.type === 'checkbox') return el.checked;
    if (el.type === 'file') {
      if (el.files && el.files.length) return el.files[0].name;
      // Workday empties the input and lists the uploaded file next to it instead.
      const up = el.closest('[data-automation-id="attachments-FileUpload"]');
      const name = up && up.querySelector('[data-automation-id="file-upload-item-name"]');
      return name ? clean(name.textContent) : '';
    }
    if (el.getAttribute('role') === 'combobox' && !el.value) {
      const box = document.querySelector(`[${BOX}="${id}"]`);
      const shownValue = box && box.querySelector('[class*="singleValue" i], [class*="single-value" i]');
      return shownValue ? clean(shownValue.innerText) : '';
    }
    return el.value;
  }

  function scan() {
    const fields = [];
    const done = new Set();
    for (const el of document.querySelectorAll(CONTROLS)) {
      if (done.has(el) || !isCandidate(el)) continue;
      const t = (el.type || '').toLowerCase();
      if (el.tagName === 'INPUT' && (t === 'radio' || t === 'checkbox')) {
        let members = [el];
        if (el.name) {
          // Same name in different blocks (Workday's "I currently work here" in every Work Experience) are separate fields.
          const blockOf = m => m.closest('fieldset, [role="radiogroup"], [role="group"], [data-automation-id^="formField-"], form');
          members = [...(el.form || document).querySelectorAll(`input[type="${t}"]`)]
            .filter(m => m.name === el.name && isCandidate(m) && blockOf(m) === blockOf(el));
        }
        if (members.length === 1 && t === 'checkbox') {
          const fs = el.closest('fieldset, [role="group"]');
          const ms = fs ? [...fs.querySelectorAll('input[type="checkbox"]')].filter(isCandidate) : [];
          if (ms.length > 1) members = ms;
        }
        members.forEach(m => done.add(m));
        const cont = container(members);
        const optLabels = members.map(m => explicitLabel(m) || clean(m.value));
        let type = t;
        let label;
        if (t === 'checkbox' && members.length === 1) {
          const outer = groupLabel(cont, members);
          if (outer && outer !== optLabels[0] && optLabels[0].length <= 40) label = outer;  // question + one option ("I understand")
          else { type = 'boolean'; label = optLabels[0] || outer; }
        } else {
          label = groupLabel(cont, members);
        }
        const id = members[0].getAttribute(ATTR) || String(++seq);
        members.forEach((m, i) => {
          m.setAttribute(ATTR, id);
          if (type !== 'boolean') { m.setAttribute(OPT, String(i)); m.setAttribute(OPT_LABEL, optLabels[i]); }
        });
        if (cont) cont.setAttribute(BOX, id);
        fields.push({ id, type, label, options: type === 'boolean' ? [] : optLabels,
                      required: isRequired(members, label, cont), value: valueOf(id), name: el.name || '' });
        continue;
      }
      done.add(el);
      const id = el.getAttribute(ATTR) || String(++seq);
      el.setAttribute(ATTR, id);
      const cont = container([el]);
      let type = el.tagName === 'SELECT' ? 'select' : el.tagName === 'TEXTAREA' ? 'textarea'
        : t === 'file' ? 'file' : t === 'date' ? 'date' : 'text';
      const role = el.getAttribute('role');
      const auto = el.getAttribute('aria-autocomplete');
      if (type === 'text' && (role === 'combobox' || auto === 'list' || auto === 'both')) {
        type = 'combobox';
        if (cont) cont.setAttribute(BOX, id);
      }
      if (el.matches(PROMPT)) {
        type = 'multiselect';
        el.setAttribute(KIND, 'multiselect');
        if (cont) cont.setAttribute(BOX, id);
      }
      let label = named(explicitLabel(el), groupLabel(cont, [el])) || nearbyText(el) || named(el.placeholder, el.name);
      if (type === 'file') {  // Workday labels its upload "Upload a file"; the group around it says what for
        const g = groupHeading(el);
        if (g && !/resume|\bcv\b|cover|transcript/i.test(label)) label = `${g}: ${label}`;
      }
      const f = { id, type, label, required: isRequired([el], label, cont), value: valueOf(id), name: el.name || '',
                  placeholder: el.placeholder || '', inputType: t, maxlength: el.maxLength > 0 ? el.maxLength : null,
                  options: [], optionValues: [] };
      if (type === 'select') {
        for (const o of el.options) {
          const txt = clean(o.text);
          if (!o.value && (!txt || /^(select|choose|please|--)/i.test(txt))) continue;
          f.options.push(txt);
          f.optionValues.push(o.value);
        }
      }
      fields.push(f);
    }
    // Yes/No button pairs (Ashby and others render booleans this way).
    for (const yes of document.querySelectorAll('button')) {
      if (yes.hasAttribute(OPT) && yes.getAttribute(OPT) !== '0') continue;
      if (!/^yes$/i.test(clean(yes.innerText)) || !shown(yes) || yes.closest('[data-jobapps-panel]')) continue;
      const btns = [...yes.parentElement.children].filter(b => b.tagName === 'BUTTON');
      if (btns.length < 2 || btns.length > 4 || !btns.some(b => /^no$/i.test(clean(b.innerText)))) continue;
      const id = btns[0].getAttribute(ATTR) || String(++seq);
      btns.forEach((b, i) => { b.setAttribute(ATTR, id); b.setAttribute(OPT, String(i)); b.setAttribute(OPT_LABEL, clean(b.innerText)); });
      const cont = container(btns);
      if (cont) cont.setAttribute(BOX, id);
      const label = groupLabel(cont, btns);
      fields.push({ id, type: 'buttons', label, options: btns.map(b => clean(b.innerText)),
                    required: isRequired(btns, label, cont), value: valueOf(id), name: '' });
    }
    // Split dates (Workday: month / year spin boxes) as one field.
    for (const wrap of document.querySelectorAll(DATE_WRAP)) {
      if (!shown(wrap) || wrap.closest('[data-jobapps-panel]')) continue;
      const parts = DATE_PARTS.filter((p) => datePart(wrap, p));
      if (!parts.length) continue;
      const id = wrap.getAttribute(ATTR) || String(++seq);
      wrap.setAttribute(ATTR, id);
      wrap.setAttribute(KIND, 'date');
      const box = wrap.closest('[data-automation-id^="formField-"]') || wrap.parentElement;
      box.setAttribute(BOX, id);
      const fs = wrap.closest('fieldset');
      const label = (fs && textOf(fs.querySelector('legend'))) || explicitLabel(wrap) || textOf(box.querySelector('label'));
      fields.push({ id, type: 'date', parts, label, required: isRequired(parts.map((p) => datePart(wrap, p)), label, box),
                    value: valueOf(id), name: '', placeholder: '', inputType: '', options: [] });
    }
    // Dropdown buttons whose options only exist once opened. Only ones in a form: not site menus.
    for (const btn of document.querySelectorAll(DROPDOWN)) {
      if (btn.disabled || !shown(btn) || btn.closest('[data-jobapps-panel], [data-automation-id="selectedItemList"]')) continue;
      if (!btn.closest('form, fieldset, [data-automation-id^="formField-"], [role="dialog"], main')) continue;
      const id = btn.getAttribute(ATTR) || String(++seq);
      btn.setAttribute(ATTR, id);
      btn.setAttribute(KIND, 'dropdown');
      const cont = container([btn]);
      if (cont) cont.setAttribute(BOX, id);
      const fs = btn.closest('fieldset');
      const own = labelsFor(btn).map((l) => textOf(l)).find(Boolean);
      const legend = fs && fs.querySelectorAll(DROPDOWN).length === 1 ? textOf(fs.querySelector('legend')) : '';
      // Workday's aria-label is "<label> <current value> Required"; only a last resort
      const aria = clean((btn.getAttribute('aria-label') || '').replace(/\brequired\s*$/i, '').replace(clean(btn.innerText), ''));
      const label = named(own, legend, groupLabel(cont, [btn])) || nearbyText(btn) || (PLACEHOLDER_TEXT.test(aria) ? '' : named(aria));
      fields.push({ id, type: 'dropdown', label, required: isRequired([btn], label, cont), value: valueOf(id), name: btn.name || '',
                    options: [] });
    }
    for (const f of fields) {
      // "Full name ✱" -> "Full name" (required was already worked out from the marker above)
      f.label = f.label.replace(/\s*[✱*]+\s*/g, ' ').replace(/\s+\(?(required|optional)\)?\s*$/i, '').trim();
      // Screen-reader copies: "First major Question First major" -> "First major"
      const twice = /^(.{3,120}?)\s+(?:question\s+)?\1$/i.exec(f.label);
      if (twice) f.label = twice[1];
      const el = document.querySelector(`[${ATTR}="${f.id}"]`);
      f.section = el ? sectionOf(el) : '';
    }
    return fields;
  }

  function count() {
    return [...document.querySelectorAll(CONTROLS)].filter(isCandidate).length;
  }

  function mark(id, state, note) {
    const target = document.querySelector(`[${BOX}="${id}"]`) || document.querySelector(`[${ATTR}="${id}"]`);
    if (!target) return;
    const color = COLORS[state];
    target.style.outline = color ? `2px solid ${color}` : '';
    target.style.outlineOffset = color ? '2px' : '';
    if (note) target.title = `JobApps: ${note}`;
  }

  const reducedMotion = () => window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  function focus(id) {
    const box = document.querySelector(`[${BOX}="${id}"]`) || document.querySelector(`[${ATTR}="${id}"]`);
    if (!box) return;
    box.scrollIntoView({ block: 'center', behavior: reducedMotion() ? 'auto' : 'smooth' });
    const input = document.querySelector(`[${ATTR}="${id}"]`);
    setTimeout(() => { try { if (input) input.focus({ preventScroll: true }); } catch (e) { /* not focusable */ } }, 350);
    if (!reducedMotion() && box.animate) {
      box.animate([{ boxShadow: '0 0 0 0 rgba(43,79,214,0)' }, { boxShadow: '0 0 0 7px rgba(43,79,214,.35)' },
        { boxShadow: '0 0 0 0 rgba(43,79,214,0)' }], { duration: 800, iterations: 2 });
    }
  }

  // ---------------------------------------------------------------- the panel
  // Drawn inside a shadow root so the job site's CSS can't reach it (and it can't affect the site).
  const PANEL_CSS = `
    :host { all: initial; }
    * { box-sizing: border-box; }
    .card { --bg:#ffffff; --ink:#16191f; --muted:#5b6472; --line:#e3e6eb; --soft:#f2f4f7; --accent:#2b4fd6;
      --filled:#16a34a; --draft:#2563eb; --need:#dc2626; --opt:#b45309;
      width: 384px; max-width: calc(100vw - 32px); color: var(--ink); background: var(--bg);
      font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, Roboto, sans-serif;
      border: 1px solid var(--line); border-radius: 14px; overflow: hidden;
      box-shadow: 0 18px 48px rgba(15, 23, 42, .22), 0 2px 8px rgba(15, 23, 42, .08); }
    @media (prefers-color-scheme: dark) {
      .card { --bg:#1b1e24; --ink:#eceef2; --muted:#a3abb8; --line:#2d323b; --soft:#252a32; --accent:#5b7cfa;
        --filled:#4ade80; --draft:#7ea2ff; --need:#f87171; --opt:#fbbf24; }
    }
    .inframe .card, .card.inframe { width: 100%; max-width: none; box-shadow: none; }
    header { display: flex; align-items: center; gap: 10px; padding: 12px 12px 12px 16px; background: var(--accent); color: #fff; }
    header.done { background: #15803d; }
    .mark { flex: none; width: 22px; height: 22px; border-radius: 6px; background: rgba(255,255,255,.18); display: grid; place-items: center; }
    .who { flex: 1; min-width: 0; }
    .who b { display: block; font-size: 15px; font-weight: 650; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .who span { display: block; font-size: 12.5px; opacity: .85; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .icon { flex: none; width: 28px; height: 28px; border: 0; border-radius: 7px; background: transparent; color: #fff;
      font-family: inherit; font-size: 18px; line-height: 1; cursor: pointer; display: grid; place-items: center; }
    .icon:hover { background: rgba(255,255,255,.16); }
    .icon:focus-visible, button:focus-visible { outline: 2px solid #fff; outline-offset: 1px; }
    .body { padding: 14px 16px 12px; }
    .bar { display: flex; height: 8px; border-radius: 99px; background: var(--soft); overflow: hidden; }
    .bar i { display: block; height: 100%; }
    .bar .f { background: var(--filled); } .bar .d { background: var(--draft); }
    .counts { display: flex; gap: 14px; margin: 9px 0 2px; font-size: 13px; color: var(--muted); }
    .counts b { font-size: 15px; color: var(--ink); font-variant-numeric: tabular-nums; margin-right: 3px; }
    .counts .timer { margin-left: auto; font-size: 12.5px; }
    .counts .dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; vertical-align: 1px; }
    .note { margin: 12px 0 0; padding: 9px 11px; border-radius: 9px; background: var(--soft); font-size: 13px; }
    .note.first { margin-top: 0; }
    h3 { margin: 14px 0 6px; font-size: 13px; font-weight: 650; color: var(--ink); }
    h3 small { font-weight: 400; color: var(--muted); }
    ol { list-style: none; margin: 0; padding: 0; max-height: 210px; overflow: auto; border: 1px solid var(--line); border-radius: 10px; }
    li + li { border-top: 1px solid var(--line); }
    .item { display: grid; grid-template-columns: 10px 1fr; gap: 10px; width: 100%; padding: 8px 10px; border: 0; background: transparent;
      color: var(--ink); font: inherit; font-size: 13.5px; text-align: left; cursor: pointer; }
    .item:hover { background: var(--soft); }
    .item .pip { width: 8px; height: 8px; margin-top: 6px; border-radius: 50%; background: var(--need); }
    .item.opt .pip { background: var(--opt); } .item.drafted .pip { background: var(--draft); }
    .item .label { display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
    .item .why { display: block; font-size: 12px; color: var(--muted); margin-top: 1px; }
    .actions { display: flex; gap: 8px; margin-top: 14px; }
    .btn { flex: 1; padding: 9px 12px; border-radius: 9px; border: 1px solid var(--line); background: var(--bg); color: var(--ink);
      font: inherit; font-size: 14px; font-weight: 600; cursor: pointer; }
    .btn:hover { background: var(--soft); }
    .btn.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
    .btn.primary:hover { filter: brightness(1.07); }
    .btn:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
    .foot { margin: 10px 0 0; font-size: 12px; color: var(--muted); }
    .pill { display: inline-flex; align-items: center; gap: 8px; padding: 9px 14px 9px 10px; border: 0; border-radius: 99px;
      background: var(--accent); color: #fff; font: 600 14px/1 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
      box-shadow: 0 10px 30px rgba(15,23,42,.25); cursor: pointer; }
    .pill .count { background: #fff; color: var(--accent); border-radius: 99px; padding: 3px 8px; font-size: 12.5px; }
  `;
  const panelState = { minimized: false, closed: false };
  let panelSheet = null;
  // The panel's element and its shadow root. The root is closed, so scripts on the job site can't reach the
  // panel's buttons; we keep our own references instead of looking it up in the page.
  let panelHost = null;
  let panelRoot = null;
  const panelShown = () => !!(panelHost && panelHost.isConnected);

  function h(tag, attrs = {}, ...children) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (k === 'onclick') el.onclick = v; else if (v !== false && v != null) el.setAttribute(k, v === true ? '' : v);
    }
    for (const c of children) if (c != null && c !== false) el.append(c);
    return el;
  }

  const CHECK = () => {
    const s = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    s.setAttribute('viewBox', '0 0 24 24'); s.setAttribute('width', '15'); s.setAttribute('height', '15');
    const p = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    p.setAttribute('d', 'M5 12.5l4.5 4.5L19 7.5'); p.setAttribute('fill', 'none'); p.setAttribute('stroke', 'currentColor');
    p.setAttribute('stroke-width', '3'); p.setAttribute('stroke-linecap', 'round'); p.setAttribute('stroke-linejoin', 'round');
    s.append(p);
    return s;
  };

  // data: {title, subtitle?, filled, drafts, todo: [{id,label,required,note}], drafted?: [{id,label}], note?,
  //        buttons?: [{id,label,primary?}], inFrame?, done?, reopen?}
  // A button press calls window.__jobapps.onAction(id), which content.js sets. That callback lives in the
  // extension's isolated world: a script on the page can neither call it nor fake a press (no window event, a
  // closed shadow root, and only real clicks count). Closing the panel calls it with 'close'.
  function act(id, e) {
    if (e && e.isTrusted === false) return;
    const fn = window.__jobapps && window.__jobapps.onAction;
    if (typeof fn === 'function') fn(id);
  }
  let lastPanel = null;
  let follower = null;

  // Some sites (Remix/React apps that render the whole document) remove nodes they didn't create.
  function ensurePanel() {
    if (lastPanel && !panelState.closed && !panelShown()) panel({ ...lastPanel, reopen: false });
  }

  // Inside an embedded form (a tall iframe that never scrolls itself) "position: fixed" means the
  // bottom of the whole form, far out of sight. The iframe can still learn which part of it is on
  // screen from an IntersectionObserver, so keep the panel at the bottom of that visible part.
  // Observing the whole document only reports changes at its edges, so a column of thin invisible
  // markers tells us which slice is on screen as the outer page scrolls.
  let followTimer = null;
  let followRail = null;
  function unfollow() {
    if (follower) follower.disconnect();
    clearInterval(followTimer);
    if (followRail) followRail.remove();
    follower = null;
    followTimer = null;
    followRail = null;
  }

  function followViewport() {
    if (follower || !('IntersectionObserver' in window)) return;
    const STEP = 80;
    const visible = new Map();
    const rail = document.createElement('div');
    rail.setAttribute('data-jobapps-rail', '');
    rail.style.cssText = 'all:initial;position:absolute;left:0;top:0;width:1px;pointer-events:none;opacity:0;';
    followRail = rail;
    const place = () => {
      const host = panelShown() ? panelHost : null;
      if (!host || !visible.size) return;
      let top = Infinity;
      let bottom = -Infinity;
      for (const r of visible.values()) { top = Math.min(top, r.top); bottom = Math.max(bottom, r.bottom); }
      const y = Math.max(top + 12, bottom - host.offsetHeight - 16) + window.scrollY;
      host.style.top = `${Math.round(Math.max(8, y))}px`;
    };
    follower = new IntersectionObserver((entries) => {
      for (const e of entries) {
        if (e.isIntersecting) visible.set(e.target, e.intersectionRect); else visible.delete(e.target);
      }
      place();
    }, { threshold: [0, 0.25, 0.5, 0.75, 1] });
    let builtFor = 0;
    const build = () => {
      if (!rail.isConnected) document.documentElement.append(rail);
      const height = document.documentElement.scrollHeight;
      if (Math.abs(height - builtFor) < STEP) return;
      builtFor = height;
      follower.disconnect();
      visible.clear();
      rail.replaceChildren();
      for (let y = 0; y < height; y += STEP) {
        const s = document.createElement('div');
        s.style.cssText = `position:absolute;left:0;top:${y}px;width:1px;height:${STEP}px;`;
        rail.append(s);
        follower.observe(s);
      }
    };
    build();
    followTimer = setInterval(build, 2000);  // the form grows as conditional questions appear
  }

  // An embedded form either scrolls by itself (fixed positioning works) or is sized to its content
  // and scrolled by the outer page (needs followViewport).
  const frameScrollsItself = () => document.documentElement.scrollHeight > window.innerHeight + 40;

  function panel(data) {
    if (window.top !== window && !data.inFrame) return;
    lastPanel = data;
    if (data.reopen) panelState.closed = false;
    if (panelState.closed) { closePanel(); return; }
    if (!panelHost) {
      panelHost = document.createElement('div');
      panelHost.setAttribute('data-jobapps-panel', '');
      panelRoot = panelHost.attachShadow({ mode: 'closed' });
    }
    const host = panelHost;
    if (!host.isConnected) document.documentElement.append(host);  // first time, or the site removed it
    const keepTop = host.style.top;
    const follow = data.inFrame && !frameScrollsItself();
    host.style.cssText = follow
      ? `all:initial;position:absolute;right:16px;top:${keepTop || '12px'};z-index:2147483647;`
      : 'all:initial;position:fixed;right:20px;bottom:20px;z-index:2147483647;';
    if (follow) followViewport(); else unfollow();
    const root = panelRoot;
    if (!panelSheet && 'adoptedStyleSheets' in root) {
      panelSheet = new CSSStyleSheet();
      panelSheet.replaceSync(PANEL_CSS);  // a constructed sheet isn't blocked by the site's CSP
    }
    if (panelSheet) root.adoptedStyleSheets = [panelSheet];
    root.replaceChildren();
    if (!panelSheet) root.append(h('style', {}, PANEL_CSS));

    const todo = data.todo || [];
    const drafted = data.drafted || [];
    const need = todo.filter((t) => t.required).length;
    if (panelState.minimized) {
      const label = data.done ? 'Marked applied' : todo.length ? 'need you' : 'all filled';
      root.append(h('button', { class: 'pill', title: 'Show JobApps', onclick: () => { panelState.minimized = false; panel(data); } },
        CHECK(), 'JobApps', h('span', { class: 'count' }, data.done ? '✓' : todo.length ? `${todo.length} ${label}` : label)));
      return;
    }

    const total = (data.filled || 0) + (data.drafts || 0) + todo.length;
    const pct = (n) => `${total ? (100 * n) / total : 0}%`;
    const close = (e) => { closePanel(); act('close', e); };
    const closeBtn = h('button', { class: 'icon', title: 'Close (the JobApps toolbar button brings it back)', 'aria-label': 'Close',
      onclick: close }, '×');
    if (data.compact) {  // an offer or a heads-up before any filling: no counts yet
      const head = h('header', {}, h('span', { class: 'mark' }, CHECK()),
        h('div', { class: 'who' }, h('b', {}, data.title || 'JobApps'), h('span', {}, data.subtitle || 'JobApps autofill')), closeBtn);
      const body = h('div', { class: 'body' }, data.note ? h('p', { class: 'note first' }, data.note) : null);
      if (data.buttons && data.buttons.length) {
        body.append(h('div', { class: 'actions' }, ...data.buttons.map((b) => h('button', { class: `btn ${b.primary ? 'primary' : ''}`,
          onclick: (e) => act(b.id, e) }, b.label))));
      }
      root.append(h('section', { class: 'card' }, head, body));
      return;
    }
    const header = h('header', { class: data.done ? 'done' : '' },
      h('span', { class: 'mark' }, CHECK()),
      h('div', { class: 'who' }, h('b', {}, data.title || 'JobApps'), h('span', {}, data.subtitle || 'JobApps autofill')),
      h('button', { class: 'icon', title: 'Minimize', 'aria-label': 'Minimize',
        onclick: () => { panelState.minimized = true; panel(data); } }, '–'),
      closeBtn);

    const body = h('div', { class: 'body' },
      h('div', { class: 'bar', role: 'img', 'aria-label': `${data.filled} filled, ${data.drafts} drafted, ${todo.length} need you` },
        h('i', { class: 'f', style: `width:${pct(data.filled || 0)}` }), h('i', { class: 'd', style: `width:${pct(data.drafts || 0)}` })),
      h('div', { class: 'counts' },
        h('span', {}, h('i', { class: 'dot', style: 'background:var(--filled)' }), h('b', {}, String(data.filled || 0)), 'filled'),
        h('span', {}, h('i', { class: 'dot', style: 'background:var(--draft)' }), h('b', {}, String(data.drafts || 0)), 'drafted'),
        h('span', {}, h('i', { class: 'dot', style: 'background:var(--need)' }), h('b', {}, String(todo.length)), 'need you'),
        data.timer ? h('span', { class: 'timer', title: 'Time you have spent working on this application' }, data.timer) : null),
      data.note ? h('p', { class: 'note' }, data.note) : null);

    if (todo.length) {
      body.append(h('h3', {}, 'Needs you ', need ? h('small', {}, `${need} required`) : null),
        h('ol', {}, ...todo.map((t) => h('li', {}, h('button', { class: `item ${t.required ? '' : 'opt'}`, title: t.note || '',
          onclick: () => focus(t.id) }, h('span', { class: 'pip' }),
          h('span', {}, h('span', { class: 'label' }, t.label || 'Unlabelled field'), t.note ? h('span', { class: 'why' }, t.note) : null))))));
    }
    if (drafted.length) {
      body.append(h('h3', {}, 'Drafted answers ', h('small', {}, 'read before you submit')),
        h('ol', {}, ...drafted.map((d) => h('li', {}, h('button', { class: 'item drafted', onclick: () => focus(d.id) },
          h('span', { class: 'pip' }), h('span', {}, h('span', { class: 'label' }, d.label)))))));
    }
    if (data.buttons && data.buttons.length) {
      body.append(h('div', { class: 'actions' }, ...data.buttons.map((b) => h('button', { class: `btn ${b.primary ? 'primary' : ''}`,
        onclick: (e) => act(b.id, e) }, b.label))));
    }
    body.append(h('p', { class: 'foot' }, data.done
      ? 'Saved on your dashboard.'
      : todo.length
        ? 'Answer the red ones on the page: JobApps remembers what you enter for next time. Then press the site’s own Submit button.'
        : 'Check the highlighted fields, then press the site’s own Submit button. JobApps never submits for you.'));
    root.append(h('section', { class: 'card' }, header, body));
  }

  function watch() {
    if (watching) return;
    watching = true;
    const send = target => {
      const el = target && target.closest && target.closest(`[${ATTR}]`);
      if (!el) return;
      const id = el.getAttribute(ATTR);
      setTimeout(() => {
        try { if (window.__jobappsRecord) window.__jobappsRecord({ id, value: valueOf(id) }); } catch (e) { /* page navigating */ }
      }, 250);
    };
    document.addEventListener('change', e => send(e.target), true);
    document.addEventListener('focusout', e => send(e.target), true);
    document.addEventListener('click', e => {
      const t = e.target;
      if (!t || !t.closest) return;
      if (t.closest(`button[${OPT}]`)) send(t.closest(`button[${OPT}]`));
      else if (t.closest('[role="option"]')) send(document.activeElement);
    }, true);
  }

  function closePanel() {
    panelState.closed = true;
    unfollow();
    if (panelHost) panelHost.remove();
  }

  // onAction: set by content.js (see act above).
  window.__jobapps = { scan, count, mark, focus, panel, ensurePanel, closePanel, watch, valueOf, onAction: null };
})();
