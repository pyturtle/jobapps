'use strict';

const $ = (sel, el = document) => el.querySelector(sel);
const h = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...opts });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw Object.assign(new Error(data.error || res.statusText), { status: res.status });
  return data;
}
const post = (path, body) => api(path, { method: 'POST', body: JSON.stringify(body || {}) });

let toastTimer;
function toast(msg, ms = 2400) {
  const t = $('#toast');
  t.textContent = msg;
  t.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove('show'), ms);
}

let STATE = null;
let pollTimer = null;
let pendingRender = false;
let openJob = null;          // the job whose details are showing (one at a time)
const openState = {};        // remembers which <details> you opened or closed across redraws

// ---------------------------------------------------------------- tabs

function setTab(tab) {
  document.querySelectorAll('.tabs button').forEach((b) => b.setAttribute('aria-selected', String(b.dataset.tab === tab)));
  document.querySelectorAll('.tab').forEach((s) => { s.hidden = s.id !== `tab-${tab}`; });
  if (location.hash !== `#${tab}`) history.replaceState(null, '', `#${tab}`);
  if (tab === 'setup') loadSetup();
  if (tab === 'history') loadHistory();
  if (tab === 'postings') loadPostings();
}

// ---------------------------------------------------------------- today

let viewDay = null;          // an earlier day's list, or null for today
let loadSeq = 0;

async function loadState() {
  const prev = STATE;
  const was = prev && prev.jobs.find((j) => j.id === openJob);
  const seq = ++loadSeq;
  let s;
  try {
    s = await api(`/api/state${viewDay ? `?day=${viewDay}` : ''}`);
  } catch (e) {
    if (seq !== loadSeq) return;
    // A list already on screen stays there, with whatever you've typed in it
    if (STATE) toast(`Can't reach your JobApps server (${e.message}). Showing the list as it was.`, 6000);
    else $('#tab-today').innerHTML = `<div class="empty"><h2>Can't reach your JobApps server</h2><p>${h(e.message)}. Check that Tailscale is on.</p></div>`;
    return;
  }
  if (seq !== loadSeq) return; // a newer request (e.g. you clicked to another day) wins
  STATE = s;
  keepUnsaved();
  if (s.date === s.today) viewDay = null;
  const fresh = !prev || prev.date !== s.date;
  const now = fresh ? null : s.jobs.find((j) => j.id === openJob);
  // New day shown, gone from the list, or the extension just marked it applied: move on to the next one.
  if (fresh || (openJob && !now) || (was && now && was.status === 'pending' && now.status !== 'pending')) {
    openJob = ((now && nextPending(now.id)) || STATE.jobs.find((j) => j.status === 'pending') || {}).id || null;
  }
  render();
  renderRun(STATE.run);
  if (STATE.run.running) startPolling();
}

function render() {
  const active = document.activeElement;
  if (active && active.tagName === 'TEXTAREA' && $('#tab-today').contains(active)) {
    pendingRender = true; // don't pull the text box out from under you; redraw once you leave it
    return;
  }
  pendingRender = false;
  $('#tab-today').innerHTML = todayHTML(STATE);
}

const VERDICT = { strong: 'strong', good: 'good', stretch: 'stretch', 'long shot': 'long' };
const STATUS = { applied: 'Applied', skipped: 'Skipped', closed: 'Closed' };
const BOARD = { greenhouse: 'Greenhouse', lever: 'Lever', ashby: 'Ashby', workday: 'Workday', smartrecruiters: 'SmartRecruiters',
  workable: 'Workable', icims: 'iCIMS', oracle: 'Oracle', eightfold: 'Eightfold', other: 'Company site' };

function dateLabel(iso) {
  return new Date(`${iso}T12:00:00`).toLocaleDateString(undefined, { weekday: 'long', month: 'long', day: 'numeric' });
}

function shortDate(iso) {
  return new Date(`${iso.slice(0, 10)}T12:00:00`).toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' });
}

function dayTitle(s) {
  const back = Math.round((new Date(`${s.today}T12:00:00`) - new Date(`${s.date}T12:00:00`)) / 864e5);
  if (back === 0) return 'Today\'s applications';
  if (back === 1) return 'Yesterday\'s applications';
  if (back < 7) return `${new Date(`${s.date}T12:00:00`).toLocaleDateString(undefined, { weekday: 'long' })}'s applications`;
  return 'Earlier applications';
}

function todayHTML(s) {
  const jobs = s.jobs;
  const isToday = s.date === s.today;
  const pending = jobs.filter((j) => j.status === 'pending' && j.how !== 'carried').length;  // the day's own picks
  const applied = (s.totals && s.totals.applied) || 0;
  const busy = s.run.running ? 'disabled' : '';
  const dates = [...new Set([s.today, ...s.days.map((d) => d.date)])].sort();
  const at = dates.indexOf(s.date);
  const prev = dates[at - 1];
  const next = dates[at + 1];
  const nav = [
    prev ? `<button class="btn" data-day="${h(prev)}" title="Open the list from ${h(dateLabel(prev))}">‹ ${h(shortDate(prev))}</button>` : '',
    next ? `<button class="btn" data-day="${h(next)}">${h(next === s.today ? 'Today' : shortDate(next))} ›</button>` : '',
  ].join('');
  const out = [];
  // The day's main list (daily_count) and anything you added on top of it.
  const main = jobs.filter((j) => !j.extra && j.how !== 'carried');
  const extras = jobs.filter((j) => j.extra);
  const carried = jobs.filter((j) => j.how === 'carried');
  const doneOf = (list) => list.filter((j) => j.status !== 'pending').length;
  const counted = main.length ? main : carried;  // a list with no new picks yet: count what's carried
  const spent = jobs.reduce((n, j) => n + (j.seconds || 0), 0);
  out.push(`<div class="day"><div><p class="date">${h(dateLabel(s.date))}</p><h1>${h(dayTitle(s))}</h1></div>
    <div class="actions">${nav}
      ${isToday && !s.preview && jobs.length ? `${moreHTML(busy)}
      <button class="btn quiet" data-run="refresh" ${busy} title="Fetch new postings and re-read today's forms">Refresh</button>` : ''}
    </div>
    ${jobs.length && !s.preview ? `<div class="tally"><div class="steps" aria-hidden="true">${counted.map((j) => `<span class="${h(j.status)}"></span>`).join('')}
      ${extras.length ? `<i class="gap"></i>${extras.map((j) => `<span class="${h(j.status)} xtra"></span>`).join('')}` : ''}</div>
      <span class="count">${doneOf(counted)} of ${counted.length} done${extras.length ? ` <small>+ ${doneOf(extras)} of ${extras.length} extra</small>` : ''}
        <small>&nbsp; ${spent ? `${duration(spent)} today, ` : ''}${applied} applied so far</small></span></div>` : ''}
  </div>`);

  const setup = s.setup;
  if (setup.missing.length || !setup.has_material) {
    const bits = [];
    if (!setup.has_material) bits.push('upload your resume');
    if (setup.missing.length) bits.push(`answer ${setup.missing.length} profile question${setup.missing.length > 1 ? 's' : ''}`);
    out.push(`<div class="banner"><span>To match picks and answers to you, ${h(bits.join(' and '))} in Setup.</span>
      <button class="btn small" data-tab-go="setup">Open Setup</button></div>`);
  }
  if (isToday && s.unanswered) {
    const many = s.unanswered > 1;
    out.push(`<div class="banner info"><span>${s.unanswered} question${many ? 's' : ''} on application forms JobApps couldn't answer. Answer ${many ? 'them' : 'it'} once and ${many ? "they're" : "it's"} filled in for you from now on.</span>
      <button class="btn small" data-goto="answers">Answer now</button></div>`);
  }
  if (s.run.running) out.push(`<pre class="log" id="runlog">${h(s.run.lines.slice(-14).join('\n'))}</pre>`);
  if (s.run.last_error && !s.run.running) out.push(`<div class="banner bad"><span>The last run failed: ${h(s.run.last_error)}. It tries again every hour.</span></div>`);
  if (s.claude_failing) out.push(`<div class="banner bad"><span>Claude calls are failing on your server (${h(s.claude_failing)}). Run <code>claude</code> once on the PC to sign in again.</span></div>`);

  const when = s.next_run && s.next_run.startsWith(s.today) ? `at ${s.next_run.slice(11, 16)}` : 'this morning';
  if (s.preview && jobs.length) {
    out.push(`<div class="banner info"><span>Today's new picks arrive ${h(when)}. Until then, here ${jobs.length > 1 ? `are the ${jobs.length} jobs` : 'is the job'} you haven't finished from earlier days. Unfinished jobs go on today's list first.</span>
      <button class="btn small" data-run="today" ${busy}>Build today's list now</button></div>`);
  } else if (!isToday && pending) {
    out.push(`<p class="note">Jobs you haven't finished here carry over to today's list, up to ${s.daily_count} a day.</p>`);
  }
  if (!jobs.length) {
    out.push(isToday
      ? `<div class="empty"><h2>No jobs picked yet today</h2><p>Your list is built every morning${h(when === 'this morning' ? '' : ` ${when}`)}. You can build it now.</p>
        <button class="btn primary" data-run="today" ${busy}>Build today's list</button></div>`
      : '<div class="empty"><h2>Nothing on this day\'s list</h2></div>');
  } else {
    if (isToday && !s.preview && !pending && main.length) {
      out.push(`<div class="empty"><h2>All done for today</h2><p>Your next list arrives tomorrow morning. On a roll? Keep going with the next best fits.</p>
        <div class="more-row">${moreHTML(busy, true)}</div><p class="small">Or pick any posting yourself in <button class="linkish" data-tab-go="postings">Postings</button>.</p></div>`);
    }
    const today5 = [...main, ...extras];
    if (today5.length) out.push(`<div class="docket">${today5.map((j, i) => jobHTML(j, i + 1)).join('')}</div>`);
    if (carried.length) {
      out.push(`${s.preview ? '' : `<h2 class="sub carried-h">Still open from earlier days <small>${carried.length}, on top of today's new picks, each for up to ${s.carry_days === 1 ? '1 day' : `${Number(s.carry_days) || 3} days`}</small></h2>`}
        <div class="docket">${carried.map((j, i) => jobHTML(j, i + 1)).join('')}</div>`);
    }
  }

  const extra = [];
  if (isToday && s.sources && s.sources.sources) {
    const src = s.sources;
    extra.push(`<details data-key="sources" ${openAttr('sources', false)}><summary>Where today's postings came from (${src.sources.length} sources${src.all_ok ? '' : ', some failed'})</summary><ul>
      ${src.sources.map((x) => `<li><strong>${h(x.source)}</strong>: ${x.active} open postings from ${x.companies} companies, ${x.canada} in Canada</li>`).join('')}</ul></details>`);
  }
  if (s.excluded && s.excluded.length) {
    extra.push(`<details data-key="excluded" ${openAttr('excluded', false)}><summary>Ruled out ${isToday ? 'today' : 'that day'} (${s.excluded.length})</summary><ul>
      ${s.excluded.map((e) => `<li><strong>${h(e.company)}</strong>, ${h(e.title)}: ${h(e.reason)}${e.quote ? `<q>${h(e.quote)}</q>` : ''}</li>`).join('')}</ul></details>`);
  }
  if (extra.length) out.push(`<div class="extra">${extra.join('')}</div>`);
  return out.join('');
}

// "Add 1 / 3 / 5 more": extras on top of the day's main list, the next best fits.
function moreHTML(busy, primary) {
  return `<span class="more ${primary ? 'big' : ''}" role="group" aria-label="Add more jobs to today">Add
    ${[1, 3, 5].map((n) => `<button class="btn ${primary && n === 3 ? 'primary' : ''}" data-run="more" data-n="${n}" ${busy}>${n}</button>`).join('')} more</span>`;
}

// 754 s -> "13 min"; 4000 s -> "1 h 7 min"
function duration(sec) {
  const m = Math.max(1, Math.round(sec / 60));
  return m < 60 ? `${m} min` : `${Math.floor(m / 60)} h ${m % 60} min`;
}

function openAttr(key, fallback) {
  return (key in openState ? openState[key] : fallback) ? 'open' : '';
}

function ago(days) {
  if (days == null) return '';
  return days === 0 ? 'posted today' : days === 1 ? 'posted yesterday' : `posted ${days} days ago`;
}

function fitBadge(pick) {
  if (!pick) return '<span class="fit" aria-hidden="true"></span>';
  const v = VERDICT[pick.verdict] || '';
  const score = pick.screen && pick.screen.fit != null ? pick.screen.fit
    : pick.fit && pick.fit.coverage != null ? Math.round(pick.fit.coverage * 100) : null;
  if (score == null) return `<span class="fit ${v}"><b>–</b><small>fit</small></span>`;
  return `<span class="fit ${v}" title="${h(pick.verdict || '')} fit, checked against your resume"><b>${score}</b><small>fit</small></span>`;
}

function jobHTML(j, n) {
  const pick = j.pick || {};
  const done = j.status !== 'pending';
  const open = openJob === j.id;
  const places = j.locations.slice(0, 2).join(' / ') + (j.locations.length > 2 ? ` +${j.locations.length - 2}` : '');
  const meta = [places, ago(j.age_days), BOARD[j.ats] || j.ats,
    j.carried && !done ? (j.first_assigned ? `on your list since ${shortDate(j.first_assigned)}` : 'from an earlier day') : '',
    j.source === 'claude-search' ? 'found by Claude search' : '',
    !done && j.seconds ? `${duration(j.seconds)} so far` : ''].filter(Boolean);
  const tag = j.how === 'added' ? '<span class="tag">Added by you</span>' : j.extra ? '<span class="tag">Extra</span>' : '';
  const took = done && j.seconds ? ` in ${duration(j.seconds)}` : '';
  const side = done
    ? `<span class="done-chip ${h(j.status)}">${STATUS[j.status] || h(j.status)}${j.status === 'applied' ? took : ''}<button class="btn small quiet undo" data-status="pending" data-id="${h(j.id)}">Undo</button></span>`
    : `<a class="btn primary open-app" href="${h(j.url)}" target="_blank" rel="noopener" data-opened="${h(j.id)}">Open application</a>`;
  return `<article class="job ${open ? 'open' : ''} ${done ? 'done' : ''}" id="job-${h(j.id)}">
    <div class="row">
      <button class="toggle" data-toggle="${h(j.id)}" aria-expanded="${open}" aria-controls="detail-${h(j.id)}"
        aria-label="${open ? 'Hide' : 'Show'} details for ${h(j.company)}"></button>
      <span class="idx">${done && j.status === 'applied' ? '✓' : n || j.position}</span>
      <span class="main"><span class="co">${h(j.company)}${tag}</span><span class="ti">${h(j.title)}</span>
        <span class="meta">${meta.map((m) => `<span>${h(m)}</span>`).join('')}</span></span>
      ${fitBadge(j.pick)}
      <span class="side">${side}</span>
    </div>
    ${open ? `<div class="detail" id="detail-${h(j.id)}">${answersHTML(j)}${whyHTML(j)}${planHTML(j)}${promptHTML(j)}
      ${done ? '' : `<div class="foot-actions">
        <button class="btn" data-status="applied" data-id="${h(j.id)}">Mark applied</button>
        <button class="btn quiet" data-status="skipped" data-id="${h(j.id)}">Skip this one</button>
        <span class="hint">The browser extension fills the form when you open it.</span></div>`}
    </div>` : ''}
  </article>`;
}

function limitOf(limit) {
  const m = /max (\d+) (words|characters)/.exec(limit || '');
  return m ? { n: Number(m[1]), unit: m[2] } : null;
}
function countText(text, lim) {
  if (!lim) return `${(text || '').length} characters`;
  const used = lim.unit === 'words' ? (text.trim() ? text.trim().split(/\s+/).length : 0) : text.length;
  return `<span class="${used > lim.n ? 'over' : ''}">${used} / ${lim.n} ${lim.unit}</span>`;
}

function checksHTML(q) {
  const c = q.checks;
  if (q.edited) {
    // The draft's checks were about the draft. After your edit, the ones the text itself still shows are kept.
    const text = q.answer || '';
    const out = ['<span class="check ok">Edited by you</span>'];
    (text.match(/\[[^\]]{2,120}\]/g) || []).forEach((p) => out.push(`<span class="check">Fill in ${h(p)}</span>`));
    ((c && c.unsupported_numbers) || []).filter((n) => text.includes(n))
      .forEach((n) => out.push(`<span class="check">“${h(n)}” isn't in your resume</span>`));
    return out.join('');
  }
  if (!q.answer || !c) return '';
  const out = [];
  if (c.unsupported_facts && c.unsupported_facts.length) {
    out.push(`<span class="check" title="${h(c.unsupported_facts.join('\n'))}">${c.unsupported_facts.length} claim${c.unsupported_facts.length > 1 ? 's' : ''} not in your resume</span>`);
  }
  (c.unsupported_numbers || []).forEach((n) => out.push(`<span class="check">“${h(n)}” isn't in your resume</span>`));
  (c.placeholders || []).forEach((p) => out.push(`<span class="check">Fill in ${h(p)}</span>`));
  if (c.unsourced_points && c.unsourced_points.length) {
    out.push(`<span class="check" title="${h(c.unsourced_points.join(' / '))}">Says something about the company that isn't in the posting</span>`);
  }
  if (!out.length) out.push('<span class="check ok">Every fact is from your resume</span>');
  if (c.generic_phrases && c.generic_phrases.length) {
    out.push(`<span class="check" title="Recruiters skim past stock phrasing like this">Sounds generic: “${h(c.generic_phrases.slice(0, 3).join('”, “'))}”</span>`);
  }
  // Shape: the lines you used to delete by hand.
  if (c.fit_closer) out.push(`<span class="check" title="${h(c.fit_closer)}">Ends on a line about fitting the role</span>`);
  if (c.self_verdicts && c.self_verdicts.length) {
    out.push(`<span class="check" title="Say what you did and let the reader judge">Praises you: “${h(c.self_verdicts.slice(0, 2).join('”, “'))}”</span>`);
  }
  if (c.meta && c.meta.length) out.push(`<span class="check">Talks about the application: “${h(c.meta.slice(0, 2).join('”, “'))}”</span>`);
  if (c.stat_stacks && c.stat_stacks.length) out.push(`<span class="check" title="${h(c.stat_stacks[0])}">Piles numbers into one sentence</span>`);
  else if (c.many_numbers) out.push(`<span class="check">Uses ${c.number_count} numbers</span>`);
  if (c.repeats && c.repeats.length) {
    out.push(`<span class="check" title="${h(c.repeats.join('\n'))}">Repeats “${h(c.repeats[0].length > 40 ? c.repeats[0].slice(0, 40) + '…' : c.repeats[0])}” from another answer</span>`);
  }
  if (c.trimmed_closer) out.push(`<span class="check ok" title="${h(c.trimmed_closer)}">Cut its closing line about fit</span>`);
  return out.join('');
}

// The posting lines an answer speaks to (quoted by the drafter, checked against the posting).
function speaksHTML(q) {
  const pts = !q.edited && q.checks && q.checks.addresses;
  return pts && pts.length ? `<p class="speaks">Answers the posting's ${pts.map((x) => `“${h(x)}”`).join(', ')}</p>` : '';
}

const previous = {};   // `${job}|${qkey}` -> the text before the last redraft, so you can put it back
const asks = {};       // `${job}|${qkey}` -> what you've typed in its "change this" box (survives redraws)
const unsaved = {};    // `${job}|${qkey}` -> { job, qkey, text } typed into an answer and not on the server yet
const redrafting = new Set();

function askHTML(j, q, id) {
  const k = `${j.id}|${q.qkey}`;
  const key = `ask:${k}`;
  const busy = redrafting.has(k);
  return `<details class="ask" data-key="${h(key)}" ${busy || openAttr(key, false) ? 'open' : ''}>
    <summary>Ask Claude to change this answer</summary>
    <div class="ask-box">
      <textarea id="ask-${id}" data-ask="${h(k)}" rows="2" ${busy ? 'disabled' : ''} placeholder="e.g. Lead with my internship project instead of the hackathon one. It's my biggest accomplishment.">${h(asks[k] || '')}</textarea>
      <div class="ask-foot">
        <label class="remember"><input type="checkbox" id="rem-${id}" ${busy ? 'disabled' : ''}> Remember this for future answers</label>
        <button class="btn small primary" data-redraft-one="${id}" data-job="${h(j.id)}" data-qkey="${h(q.qkey)}" ${busy ? 'disabled' : ''}>
          ${busy ? 'Redrafting… about 30 seconds' : 'Redraft this answer'}</button>
      </div>
      ${q.prompt ? `<details class="sub-prompt"><summary>Prompt used for this redraft</summary><pre class="prompt">${h(q.prompt)}</pre></details>` : ''}
    </div>
  </details>`;
}

function answersHTML(j) {
  if (!j.readable && !j.questions.length) {
    return '<div class="part"><h3>Your answers</h3><p class="note">This site\'s questions can only be read once the form is open. The extension reads them and drafts answers from your resume there; they show up here afterwards.</p></div>';
  }
  if (!j.questions.length) return ''; // the plan heading says there's nothing to write
  const drafted = j.questions.filter((q) => q.answer).length;
  const qs = j.questions.map((q, i) => {
    const lim = limitOf(q.limit);
    const id = `ans-${j.id}-${i}`;
    const k = `${j.id}|${q.qkey}`;
    const prev = previous[k];
    return `<div class="q"><p class="q-label">${h(q.label)}<small>${h([q.required ? 'Required' : 'Optional', q.limit].filter(Boolean).join(', '))}</small></p>
      <textarea id="${id}" data-job="${h(j.id)}" data-qkey="${h(q.qkey)}" data-limit="${h(q.limit || '')}" ${redrafting.has(k) ? 'readonly' : ''} placeholder="No draft yet. Write it here, or press Redraft once your resume is uploaded.">${h(q.answer || '')}</textarea>
      <div class="q-foot"><span class="count" data-count="${id}">${countText(q.answer || '', lim)}</span><span class="checks" data-checks="${id}">${checksHTML(q)}</span>
        <span class="check bad" data-unsaved="${h(k)}" ${unsaved[k] && unsaved[k].failed ? '' : 'hidden'}>${h(unsavedNote(unsaved[k]))}</span>
        ${prev != null ? `<button class="btn small quiet" data-restore="${id}">Put back the previous version</button>` : ''}
        <button class="btn small" data-copy="${id}">Copy</button></div>
      ${speaksHTML(q)}${askHTML(j, q, id)}</div>`;
  }).join('');
  return `<div class="part"><h3>Your answers <small>${drafted} of ${j.questions.length} drafted from your resume, saved as you type</small></h3>${qs}
    <button class="btn small quiet" data-redraft="${h(j.id)}">Redraft all</button></div>`;
}

async function redraftOne(t) {
  const id = t.dataset.redraftOne;
  const k = `${t.dataset.job}|${t.dataset.qkey}`;
  const instruction = ($(`#ask-${id}`).value || '').trim();
  const remember = $(`#rem-${id}`).checked;
  const job = STATE.jobs.find((x) => x.id === t.dataset.job);
  const q = job && job.questions.find((x) => x.qkey === t.dataset.qkey);
  if (!q) return;
  const before = q.answer || '';
  redrafting.add(k);  // its text box is read-only until the new draft is in: nothing typed meanwhile to lose
  render();
  try {
    await sendAnswer(k);  // the redraft starts from your text as it stands
    const res = await post(`/api/job/${encodeURIComponent(t.dataset.job)}/redraft-one`, { qkey: t.dataset.qkey, instruction, remember });
    previous[k] = before;
    dropUnsaved(k);
    Object.assign(q, { answer: res.answer, checks: res.checks, edited: false });
    openState[`ask:${k}`] = false;
    delete asks[k];
    toast(remember && instruction ? 'Redrafted, and remembered for future answers' : 'Redrafted');
    redrafting.delete(k);
    await loadState();  // picks up the prompt used for it
  } catch (e) {
    redrafting.delete(k);
    toast(e.message);
    render();
  }
}

function whyHTML(j) {
  const pick = j.pick;
  if (!pick) return '';
  const fit = pick.fit || {};
  const sc = pick.screen;
  const parts = [];
  if (sc && sc.summary) {
    const sentences = sc.summary.match(/[^.!?]+[.!?]+/g) || [sc.summary];
    parts.push(`<p class="summary">${h(sentences.slice(0, 2).join(' ').trim())}</p>`);
  }
  if ((fit.matched && fit.matched.length) || (fit.missing && fit.missing.length)) {
    parts.push(`<div class="skills">${(fit.matched || []).map((x) => `<span class="skill">${h(x)}</span>`).join('')}${(fit.missing || []).map((x) => `<span class="skill miss" title="The posting lists it; your resume doesn't">${h(x)}</span>`).join('')}</div>`);
  }
  (fit.warnings || []).forEach((w) => parts.push(`<div class="warn">${h(w.reason)}${w.quote ? `: <q>${h(w.quote)}</q>` : ''}</div>`));
  // What Claude objected to that isn't a reason to rule a posting out (screen.py): yours to judge
  ((sc && sc.concerns) || []).forEach((c) => parts.push(`<div class="warn">Check this: ${h(c.reason)}${c.quote ? `: <q>${h(c.quote)}</q>` : ''}</div>`));
  // Postings JobApps couldn't read itself were looked up by Claude (research.py)
  const src = pick.source || {};
  const link = src.url ? ` (<a href="${h(src.url)}" target="_blank" rel="noopener">source</a>)` : '';
  if (src.kind === 'research') parts.push(`<p class="small">This posting's page couldn't be read, so Claude found the same posting elsewhere${link} and the fit was judged on that page.</p>`);
  if (src.kind === 'research-unverified') parts.push(`<div class="warn">This posting's page couldn't be read, so the fit was judged on Claude's summary of it${link}, not the posting's own words. It counts at half weight.</div>`);
  if (sc && sc.requirements && sc.requirements.length) {
    const sym = { met: '✓', partial: '◐', missing: '–' };
    const met = sc.requirements.filter((r) => r.status === 'met').length;
    const key = `${j.id}:evidence`;
    parts.push(`<details data-key="${h(key)}" ${openAttr(key, false)}><summary class="small">See the ${sc.requirements.length} requirements checked (${met} met, each quoting your resume)</summary>
      <ul class="reqs">${sc.requirements.map((r) => `<li><span class="mark ${h(r.status)}" aria-label="${h(r.status)}">${sym[r.status] || '·'}</span>
      <span>${h(r.requirement)}${r.kind === 'preferred' ? ' <span class="small">(nice to have)</span>' : ''}${r.evidence ? `<span class="evidence">Your resume: “${h(r.evidence)}”</span>` : ''}</span></li>`).join('')}</ul>
      <p class="small">The server found every quote in the posting or your resume (a slipped word or two is allowed, a changed number is not). Verified fit ${sc.fit ?? '–'} out of 100.${sc.dropped ? ` ${sc.dropped} unverifiable requirement${sc.dropped > 1 ? 's were' : ' was'} dropped.` : ''}${sc.downgraded ? ` ${sc.downgraded} claim${sc.downgraded > 1 ? 's' : ''} without a resume quote counted as missing.` : ''}</p></details>`);
  }
  if (!parts.length) return '';
  return `<div class="part"><h3>Why this fits you</h3>${parts.join('')}</div>`;
}

function planHTML(j) {
  if (!j.plan.length) return '';
  const label = { fill: 'auto', file: 'upload', draft: 'draft', manual: 'you', skip: 'skip' };
  const auto = j.plan.filter((p) => p.kind === 'fill' || p.kind === 'file').length;
  const you = j.plan.filter((p) => p.kind === 'manual').length;
  const key = `${j.id}:plan`;
  const tail = j.readable && !j.questions.length ? ', no written questions' : '';
  return `<details class="part" data-key="${h(key)}" ${openAttr(key, false)}><summary><h3>What gets filled in <small>${auto} automatically, ${you} by you${tail}</small></h3></summary>
    <table class="plan">${j.plan.map((p) => `<tr><td><span class="kind ${h(p.kind)}">${label[p.kind]}</span></td>
      <td>${h(p.label)}${p.required ? ' *' : ''}</td><td class="small">${h(p.kind === 'fill' || p.kind === 'file' ? p.value : p.kind === 'draft' ? 'drafted answer' : p.note || '')}</td></tr>`).join('')}</table></details>`;
}

function promptHTML(j) {
  if (!j.prompt) return '';
  const key = `${j.id}:prompt`;
  return `<details class="part" data-key="${h(key)}" ${openAttr(key, false)}><summary><h3>Prompt used for the drafts</h3></summary>
    <button class="btn small" data-copy="prompt-${h(j.id)}">Copy</button><pre class="prompt" id="prompt-${h(j.id)}">${h(j.prompt)}</pre></details>`;
}

// ---------------------------------------------------------------- run status

function renderRun(run) {
  const pill = $('#runpill');
  if (run.running) {
    const last = (run.lines[run.lines.length - 1] || '').replace(/^\d\d:\d\d:\d\d /, '');
    pill.innerHTML = `<span class="dot"></span><span>${h(run.task)}: ${h(last.slice(0, 70))}</span>`;
    pill.hidden = false;
  } else if (run.last_finished) {
    pill.innerHTML = `<span>Updated ${h(run.last_finished.slice(11, 16))}</span>`;
    pill.hidden = false;
  } else {
    pill.hidden = true;
  }
  const log = $('#runlog');
  if (log) { log.textContent = run.lines.slice(-14).join('\n'); log.scrollTop = log.scrollHeight; }
}

function startPolling() {
  if (pollTimer) return;
  pollTimer = setInterval(async () => {
    try {
      const run = await api('/api/run');
      renderRun(run);
      if (!run.running) {
        clearInterval(pollTimer);
        pollTimer = null;
        toast(run.last_error ? 'That run failed; details at the top' : 'Up to date');
        loadState();
      }
    } catch (e) { /* server restarting; keep trying */ }
  }, 1500);
}

async function startRun(mode, n) {
  const body = { today: {}, more: { more: n || 3 }, refresh: { refresh: true }, rebuild: { rebuild: true } }[mode];
  const res = await post('/api/run', body);
  if (!res.started) { toast('Already working on it'); return; }
  toast({ more: `Finding ${n || 3} more…`, rebuild: 'Re-picking with your resume…', refresh: 'Refreshing…' }[mode] || 'Building today\'s list…');
  if (STATE) { STATE.run = res.run; render(); }
  renderRun(res.run);
  startPolling();
}

// ---------------------------------------------------------------- setup

const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];

let MEMORY = null;
let scrollTo = '';

async function loadSetup() {
  try {
    const [setup, mem] = await Promise.all([api('/api/setup'), api('/api/memory').catch(() => null)]);
    MEMORY = mem;
    renderSetup(setup);
  } catch (e) {
    $('#tab-setup').innerHTML = `<div class="empty"><h2>Couldn't load Setup</h2><p>${h(e.message)}</p></div>`;
  }
}

function inputFor(f, value) {
  const todo = value === null || value === undefined || value === '' ? 'todo' : '';
  const name = `name="${h(f.key)}"`;
  if (f.kind === 'bool') {
    const v = value === true ? 'true' : value === false ? 'false' : '';
    return `<select ${name} class="${v ? '' : 'todo'}"><option value="" ${v ? '' : 'selected'}>Not set</option>
      <option value="true" ${v === 'true' ? 'selected' : ''}>Yes</option><option value="false" ${v === 'false' ? 'selected' : ''}>No</option></select>`;
  }
  if (f.kind === 'month') {
    return `<select ${name} class="${todo}"><option value="">Not set</option>${MONTHS.map((m) => `<option ${String(value) === m ? 'selected' : ''}>${m}</option>`).join('')}</select>`;
  }
  const type = f.kind === 'year' ? 'number' : 'text';
  const optional = /website|gpa|locations/.test(f.key);
  return `<input ${name} type="${type}" value="${h(value ?? '')}" class="${optional ? '' : todo}">`;
}

function when(iso) {
  if (!iso) return 'never';
  const d = new Date(iso);
  const sameDay = d.toDateString() === new Date().toDateString();
  return sameDay ? `today at ${iso.slice(11, 16)}` : `${d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })} at ${iso.slice(11, 16)}`;
}

function healthHTML(x) {
  if (!x) return '';
  const c = x.claude || {};
  const ok = !x.last_run_error && x.sources_ok !== false && !c.failing && x.claude_cli;
  const rows = [
    ['Server', `running since ${when(x.server_started)}, and restarts itself if it stops`],
    ['Daily list', `last built ${when(x.last_run)}${x.last_run_error ? `; failed: ${x.last_run_error}` : ''}; next ${when(x.next_run)}`],
    ['Sources', `${x.sources} checked ${when(x.last_fetch)}${x.sources_ok === false ? '; some failed and are retried tomorrow (nothing is marked closed)' : ', all loaded'}`],
    ['Claude', !x.claude_cli ? 'the claude CLI is not on the PC' : c.failing ? `failing: ${c.last_error}` : c.last_ok ? `working, last call ${when(c.last_ok)}` : 'ready, no calls yet since the server started'],
    ['Backups', x.backups ? `${x.backups} daily ${x.backups === 1 ? 'copy' : 'copies'} (last 7 kept), newest ${x.latest_backup}` : 'the first one is made at the next daily run'],
  ];
  return `<div class="step"><h2><span class="n ${ok ? 'ok' : 'warn'}">${ok ? '✓' : '!'}</span>System</h2>
    <dl class="kv">${rows.map(([k, v]) => `<dt>${h(k)}</dt><dd>${h(v)}</dd>`).join('')}</dl></div>`;
}

let SETUP = null;
const s_prefs = () => (SETUP ? [...SETUP.writing_preferences] : []);

function renderSetup(s) {
  SETUP = s;
  const fieldsDone = !s.missing.filter((m) => !m.startsWith('files.')).length;
  $('#tab-setup').innerHTML = `
  <div class="page-head"><h1>Setup</h1><p>Everything here lives on your home server. Picks, answers and autofill all read from it.</p></div>
  <div class="step"><h2><span class="n ${s.resume ? 'ok' : ''}">${s.resume ? '✓' : '1'}</span>Your resume</h2>
    <p class="help">The fit check reads your skills from it, and drafted answers may only use facts from it and your notes below.</p>
    <label class="drop" id="drop"><input type="file" accept="application/pdf" id="resume-input" hidden>
      ${s.resume ? `<strong>${h(s.resume.name)}</strong>, ${(s.resume.size / 1024).toFixed(0)} KB. Drop a new PDF here to replace it.` : 'Drop your resume PDF here, or click to choose it'}</label>
    ${s.skills.length ? `<p class="help" style="margin:14px 0 6px">Skills found in it (${s.skills.length})</p><div class="skills">${s.skills.map((x) => `<span class="skill">${h(x)}</span>`).join('')}</div>` : ''}
  </div>
  <div class="step"><h2><span class="n ${fieldsDone ? 'ok' : ''}">${fieldsDone ? '✓' : '2'}</span>Your details</h2>
    <p class="help">Typed into forms exactly as written. Highlighted fields are still unknown, and legal questions are never guessed.</p>
    <form class="form" id="profile-form">${s.fields.map((f) => `<label>${h(f.label)}${inputFor(f, s.values[f.key])}</label>`).join('')}</form>
    <p style="margin:14px 0 0"><button class="btn primary" id="save-profile">Save details</button></p>
  </div>
  ${memoryHTML()}
  <div class="step"><h2><span class="n ${s.history.length ? 'ok' : ''}">${s.history.length ? '✓' : '3'}</span>Work history and skills</h2>
    <p class="help">Typed into the Work Experience, Skills and Education parts of applications (Workday's My Experience page), replacing whatever the site guessed from your resume. Dates like “June 2026”; end “present” if you still work there.</p>
    <div id="jobs">${s.history.map(jobRowHTML).join('')}</div>
    <p style="margin:10px 0 18px"><button class="btn small" id="add-job">Add a job</button></p>
    <label class="field-label" for="skills-list">Skills, in order, comma-separated</label>
    <textarea class="skills-list" id="skills-list" rows="3">${h(s.skills_list.join(', '))}</textarea>
    <p style="margin:12px 0 0"><button class="btn primary" id="save-exp">Save work history and skills</button></p>
  </div>
  <div class="step"><h2><span class="n ${s.background ? 'ok' : ''}">${s.background ? '✓' : '4'}</span>Notes for answers</h2>
    <p class="help">Projects, experience and short true stories, in plain notes. The more concrete, the less you edit later.</p>
    <textarea class="bg" id="bg">${h(s.background_raw)}</textarea>
    <p style="margin:12px 0 0"><button class="btn primary" id="save-bg">Save notes</button></p>
    <h3 class="sub-h">How answers are written</h3>
    <p class="help">Instructions every draft follows, saved when you tick “Remember this for future answers” on an answer.</p>
    ${s.writing_preferences.length ? `<ul class="pref-list">${s.writing_preferences.map((x, i) => `<li><span>${h(x)}</span>
      <button class="btn small quiet" data-remove-pref="${i}">Remove</button></li>`).join('')}</ul>` : '<p class="note">None yet.</p>'}
  </div>
  <div class="step"><h2><span class="n">5</span>Autofill on your laptop</h2>
    <p class="help">A small Chrome extension fills applications in your browser using the answers on your server. Set it up once and it keeps itself up to date.</p>
    <ol class="steps">
      <li>On a Mac, open <strong>Terminal</strong> and paste this (Tailscale on):
        <div class="cmd"><code id="mac-cmd">curl -fsSL ${h(location.origin)}/mac-setup.sh | bash</code><button class="btn small" data-copy="mac-cmd">Copy</button></div>
        It puts the extension in your home folder under <code>JobApps</code> and checks your server for a new version every 15 minutes.</li>
      <li>Open <code>chrome://extensions</code>, switch on <strong>Developer mode</strong> (top right), remove any older JobApps,
        click <strong>Load unpacked</strong> and pick <code>JobApps/jobapps-extension</code> in your home folder. Pin it to the toolbar.</li>
      <li>That's it: new versions install themselves. If one is waiting, the toolbar popup shows <em>Reload JobApps</em>.</li>
    </ol>
    <p class="help" style="margin:12px 0 0">Not on a Mac? <a href="/jobapps-extension.zip">Download it</a>, unzip it and load that folder instead; you'd download again for each update.
      To stop updates later: <code>bash ~/JobApps/uninstall.sh</code></p>
  </div>
  <div class="step"><h2><span class="n">6</span>Use your changes today</h2>
    <p class="help">Today's list was picked before these changes. Re-picking swaps the jobs you haven't started for ones that fit better.</p>
    <dl class="kv"><dt>Daily list</dt><dd>built at ${h(s.daily_at)} every day</dd><dt>Claude</dt><dd>${h(s.backend === 'claude-cli' ? 'your Claude login (claude CLI)' : s.backend)}, ${h(s.model)}</dd></dl>
    <p style="margin:14px 0 0"><button class="btn primary" data-run="rebuild">Re-pick today's jobs</button></p>
  </div>
  ${healthHTML(s.health)}`;
  wireSetup();
  if (scrollTo && document.getElementById(scrollTo)) {
    document.getElementById(scrollTo).scrollIntoView({ block: 'start' });
    scrollTo = '';
  }
}

function wireSetup() {
  const drop = $('#drop');
  const input = $('#resume-input');
  const upload = async (file) => {
    if (!file) return;
    toast('Uploading…');
    try {
      const res = await fetch('/api/setup/resume', { method: 'PUT', body: await file.arrayBuffer(), headers: { 'Content-Type': 'application/pdf' } });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error);
      renderSetup(data.setup);
      toast(`Resume saved with ${data.setup.skills.length} skills found${data.repicking ? '; re-picking today\'s jobs' : ''}`);
      if (data.repicking) startPolling();
    } catch (e) { toast(`Upload failed: ${e.message}`); }
  };
  input.addEventListener('change', () => upload(input.files[0]));
  drop.addEventListener('dragover', (e) => { e.preventDefault(); drop.classList.add('over'); });
  drop.addEventListener('dragleave', () => drop.classList.remove('over'));
  drop.addEventListener('drop', (e) => { e.preventDefault(); drop.classList.remove('over'); upload(e.dataTransfer.files[0]); });
  $('#save-profile').addEventListener('click', async (e) => {
    e.preventDefault();
    const body = {};
    new FormData($('#profile-form')).forEach((v, k) => { body[k] = v; });
    try {
      const res = await post('/api/setup/profile', body);
      renderSetup(res.setup);
      toast('Details saved');
    } catch (err) { toast(`Couldn't save: ${err.message}`); }
  });
  $('#save-bg').addEventListener('click', async () => {
    try {
      const res = await fetch('/api/setup/background', { method: 'PUT', body: $('#bg').value, headers: { 'Content-Type': 'text/plain; charset=utf-8' } });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error);
      renderSetup(data.setup);
      toast('Notes saved');
    } catch (err) { toast(`Couldn't save: ${err.message}`); }
  });
  document.querySelectorAll('[data-remove-pref]').forEach((b) => b.addEventListener('click', async () => {
    const items = s_prefs().filter((_, i) => i !== Number(b.dataset.removePref));
    try {
      const res = await fetch('/api/setup/preferences', { method: 'PUT', body: JSON.stringify({ items }), headers: { 'Content-Type': 'application/json' } });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error);
      renderSetup(data.setup);
      toast('Removed');
    } catch (err) { toast(`Couldn't save: ${err.message}`); }
  }));
  $('#add-job').addEventListener('click', () => {
    $('#jobs').insertAdjacentHTML('beforeend', jobRowHTML({ title: '', company: '', location: '', start: '', end: '', description: '' }));
    $('#jobs .jobrow:last-child input').focus();
  });
  $('#jobs').addEventListener('click', (e) => {
    const b = e.target.closest('[data-remove-job]');
    if (b) b.closest('.jobrow').remove();
  });
  $('#save-exp').addEventListener('click', async () => {
    const history = [...document.querySelectorAll('#jobs .jobrow')].map((row) => Object.fromEntries(
      [...row.querySelectorAll('[data-k]')].map((el) => [el.dataset.k, el.value])));
    const skills = $('#skills-list').value.split(',').map((x) => x.trim()).filter(Boolean);
    try {
      const res = await fetch('/api/setup/experience', { method: 'PUT', body: JSON.stringify({ history, skills }), headers: { 'Content-Type': 'application/json' } });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error);
      renderSetup(data.setup);
      toast('Work history and skills saved');
    } catch (err) { toast(`Couldn't save: ${err.message}`); }
  });
}

function jobRowHTML(j) {
  const input = (k, label, extra = '') => `<label>${label}<input data-k="${k}" value="${h(j[k])}" ${extra}></label>`;
  return `<div class="jobrow"><div class="form">
      ${input('title', 'Job title')}${input('company', 'Company')}
      ${input('location', 'Location (optional)')}
      <div class="dates">${input('start', 'From', 'placeholder="June 2026"')}${input('end', 'To', 'placeholder="present"')}</div>
    </div>
    <label class="field-label">What you did<textarea data-k="description" rows="4">${h(j.description)}</textarea></label>
    <button class="btn small quiet" data-remove-job>Remove this job</button></div>`;
}

// ---------------------------------------------------------------- answers JobApps remembers

const showAnswer = (a) => (a === true ? 'Yes' : a === false ? 'No' : Array.isArray(a) ? a.join(', ') : String(a ?? ''));

function answerInput(q, id) {
  const opts = q.options || [];
  if (q.type === 'boolean') return `<select id="${id}"><option value="">Choose…</option><option value="true">Yes (tick it)</option><option value="false">No (leave it)</option></select>`;
  if (q.type === 'checkbox' && opts.length > 1) {
    return `<div class="multi" id="${id}">${opts.map((o, i) => `<label><input type="checkbox" value="${h(o)}" id="${id}-${i}"> ${h(o)}</label>`).join('')}</div>`;
  }
  if (opts.length) return `<select id="${id}"><option value="">Choose…</option>${opts.map((o) => `<option>${h(o)}</option>`).join('')}</select>`;
  return `<input id="${id}" type="text" placeholder="${/^(are|do|will|would|can|have|is|did)\b/i.test(q.label) ? 'Yes or No, or your answer' : 'Your answer'}">`;
}

function readAnswer(q, id) {
  const el = document.getElementById(id);
  if (!el) return null;
  if (el.classList.contains('multi')) return [...el.querySelectorAll('input:checked')].map((c) => c.value);
  if (q.type === 'boolean') return el.value === '' ? null : el.value === 'true';
  return el.value.trim();
}

function memoryHTML() {
  if (!MEMORY) return '';
  const open = MEMORY.unanswered;
  const learned = MEMORY.learned;
  const where = (q) => (q.to_profile ? 'Saved to your profile, so every wording of this question gets it.' : '');
  return `<div class="step" id="answers"><h2><span class="n ${open.length ? 'warn' : 'ok'}">${open.length ? open.length : '✓'}</span>Answers JobApps remembers</h2>
    <p class="help">Questions it met on forms and couldn't answer. Answer each once and it's filled in for you from then on, here or right on the form (it remembers what you type there too). Essays, uploads and consent boxes aren't listed: those you read each time.</p>
    ${open.length ? `<div class="qlist">${open.map((q, i) => `<div class="qrow">
      <div class="qtext"><b>${h(q.label)}</b><span class="small">${h([q.company, q.seen > 1 ? `seen ${q.seen} times` : '', q.required ? 'required' : ''].filter(Boolean).join(', '))}${where(q) ? `. ${h(where(q))}` : ''}</span></div>
      <div class="qans">${answerInput(q, `ua-${i}`)}
        <button class="btn small primary" data-answer="${i}">Remember</button><button class="btn small quiet" data-dismiss="${i}">Not needed</button></div>
    </div>`).join('')}</div>` : '<p class="note">Nothing waiting. New ones show up here (and on Today) when a form asks something JobApps can\'t answer.</p>'}
    <details class="remembered" ${openAttr('remembered', false) ? 'open' : ''} data-key="remembered"><summary>Everything it remembers (${learned.length})</summary>
      ${learned.length ? `<div class="qlist">${learned.map((e, i) => `<div class="qrow">
        <div class="qtext"><b>${h(e.label)}</b><span class="small">from ${h(e.learned_from === 'you' ? 'your answer here' : e.learned_from || 'a form')}${e.date ? `, ${h(shortDate(e.date))}` : ''}</span></div>
        <div class="qans"><input id="lr-${i}" type="text" value="${h(showAnswer(e.answer))}">
          <button class="btn small" data-relearn="${i}">Save</button><button class="btn small quiet" data-forget="${i}">Forget</button></div>
      </div>`).join('')}</div>` : '<p class="note">Nothing yet.</p>'}
    </details>
  </div>`;
}

async function memoryAction(t) {
  const reload = async (msg) => { MEMORY = await api('/api/memory').catch(() => MEMORY); loadSetup(); toast(msg); };
  try {
    if (t.dataset.answer) {
      const q = MEMORY.unanswered[Number(t.dataset.answer)];
      const answer = readAnswer(q, `ua-${t.dataset.answer}`);
      if (answer == null || answer === '' || (Array.isArray(answer) && !answer.length)) { toast('Type or choose an answer first'); return; }
      const r = await post('/api/memory/answer', { label: q.label, answer, type: q.type, rule: q.rule });
      scrollTo = 'answers';
      await reload(r.where.startsWith('profile') ? 'Saved to your profile' : 'Remembered for next time');
    } else if (t.dataset.dismiss) {
      await post('/api/memory/dismiss', { qkey: MEMORY.unanswered[Number(t.dataset.dismiss)].qkey });
      scrollTo = 'answers';
      await reload('Hidden; it won\'t be asked again');
    } else if (t.dataset.relearn) {
      const e = MEMORY.learned[Number(t.dataset.relearn)];
      const raw = document.getElementById(`lr-${t.dataset.relearn}`).value.trim();
      const answer = typeof e.answer === 'boolean' ? /^(yes|true|y)$/i.test(raw) : raw;
      await post('/api/memory/answer', { label: e.label, answer, type: e.type });
      openState.remembered = true;
      scrollTo = 'answers';
      await reload('Updated');
    } else if (t.dataset.forget) {
      await post('/api/memory/forget', { qkey: MEMORY.learned[Number(t.dataset.forget)].qkey });
      openState.remembered = true;
      scrollTo = 'answers';
      await reload('Forgotten');
    }
  } catch (err) { toast(err.message); }
}

// ---------------------------------------------------------------- history

function daysHTML(days) {
  if (!days.length) return '';
  return `<h2 class="sub">Daily lists</h2><div class="daylist">${days.map((d) => {
    const left = d.total - d.done;
    const bar = [...Array(d.applied).fill('applied'), ...Array(d.done - d.applied).fill('other'), ...Array(left).fill('')];
    return `<button data-day="${h(d.date)}"><span class="when">${h(dateLabel(d.date))}</span>
      <span class="mini" aria-hidden="true">${bar.map((c) => `<span class="${c}"></span>`).join('')}</span>
      <span class="tot">${left ? `${left} not finished` : 'all done'}</span></button>`;
  }).join('')}</div>`;
}

function statsHTML(st) {
  if (!st || !st.applied) return '';
  if (!st.timed) {
    return `<div class="stats"><p class="small">Time per application shows here once you apply with the extension:
      it counts the minutes you spend working on each application page (not time the tab sits idle).</p></div>`;
  }
  const site = st.by_site.map((x) => `${h(BOARD[x.ats] || x.ats)} ${x.average} min (${x.count})`).join(', ');
  return `<div class="stats">
    <div class="tiles">
      <div><b>${st.average} min</b><span>average per application</span></div>
      <div><b>${st.median} min</b><span>median</span></div>
      <div><b>${duration(st.total_minutes * 60)}</b><span>total, ${st.timed} timed of ${st.applied} applied</span></div>
    </div>
    <p class="small">By site: ${site}. Fastest: ${h(st.fastest.company)}, ${st.fastest.minutes} min. Longest: ${h(st.slowest.company)}, ${st.slowest.minutes} min.</p>
  </div>`;
}

async function loadHistory() {
  const [rows, days, st] = await Promise.all([api('/api/history').catch(() => []), api('/api/days').catch(() => []),
    api('/api/stats').catch(() => null)]);
  const applied = rows.filter((r) => r.status === 'applied').length;
  const mins = Object.fromEntries(((st && st.applications) || []).map((a) => [a.id, a.minutes]));
  $('#tab-history').innerHTML = `<div class="page-head"><h1>History</h1><p>${applied} applied, ${rows.length - applied} skipped or closed.
    Every posting ever collected is under <button class="linkish" data-tab-go="postings">Postings</button>.</p></div>
    ${statsHTML(st)}
    ${daysHTML(days)}
    ${rows.length ? `<h2 class="sub">Applied and skipped</h2><div class="hlist">${rows.map((r) => `<div class="hrow">
      <div class="hline"><span class="hdate">${h(r.updated_at ? shortDate(r.updated_at) : '')}</span>
        <span class="pchip ${h(r.status)}">${h(STATUS[r.status] || r.status)}</span>
        <span class="hmain">${r.url ? `<a class="co" href="${h(r.url)}" target="_blank" rel="noopener">${h(r.company)}</a>` : `<span class="co">${h(r.company)}</span>`}
          <span class="ti">${h(r.title)}</span></span>
        <span class="htime">${r.status === 'applied' && mins[r.job_id] ? `${mins[r.job_id]} min` : ''}</span>
        <button class="btn small" data-record="${h(r.job_id)}" aria-expanded="false">Record</button></div>
      <div class="hrecord" id="rec-${h(r.job_id)}" hidden></div></div>`).join('')}</div>`
    : days.length ? '' : '<div class="empty"><h2>Nothing yet</h2><p>Your daily lists and the jobs you apply to or skip show up here.</p></div>'}`;
}

// One application's record: its links, your answers, the saved copy of the posting, and what happened when.
const EVENT = { picked: 'Picked for your list', carried: 'Carried over', added: 'Added by you', released: 'Swapped out by a re-pick',
  ruled_out: 'Ruled out', opened: 'Opened', applied: 'Applied', skipped: 'Skipped', closed: 'Posting closed', reopened: 'Undone',
  corrected: 'Record corrected' };
const eventWhy = (d) => (d ? d.reason || d.why || d.dropped || '' : '');

async function toggleRecord(btn) {
  const box = document.getElementById(`rec-${btn.dataset.record}`);
  const open = box.hidden;
  box.hidden = !open;
  btn.setAttribute('aria-expanded', String(open));
  btn.textContent = open ? 'Hide record' : 'Record';
  if (!open || box.dataset.loaded) return;
  box.innerHTML = '<p class="small">Loading…</p>';
  try {
    const r = await api(`/api/posting/${encodeURIComponent(btn.dataset.record)}`);
    const applied = [...r.events].reverse().find((e) => e.kind === 'applied');
    const where = applied && applied.detail && applied.detail.url;
    const stamp = (e) => (e.at && e.at.length > 10 ? `${shortDate(e.at)}, ${e.at.slice(11, 16)}` : shortDate(e.day || e.at));
    box.innerHTML = `
      <p class="links"><a href="${h(r.url)}" target="_blank" rel="noopener">The posting</a>
        ${where && where !== r.url ? `<a href="${h(where)}" target="_blank" rel="noopener">The page you applied on</a>` : ''}
        ${r.minutes ? `<span class="small">${r.minutes} min spent</span>` : ''}</p>
      <ol class="timeline">${r.events.map((e) => `<li><span>${h(stamp(e))}</span>${h(EVENT[e.kind] || e.kind)}${eventWhy(e.detail) ? `: ${h(eventWhy(e.detail))}` : ''}</li>`).join('')}</ol>
      ${r.answers.length ? `<h3 class="sub-h">Your answers</h3>${r.answers.map((a) => `<div class="qa"><b>${h(a.question)}</b><p>${h(a.answer)}</p></div>`).join('')}` : ''}
      <h3 class="sub-h">The posting, as saved${r.saved_at ? ` ${h(shortDate(r.saved_at))}` : ''}</h3>
      ${r.description ? `<pre class="saved">${h(r.description)}</pre>` : '<p class="small">No copy saved yet; it\'s fetched in the background after you apply. The link above still works while the posting is up.</p>'}`;
    box.dataset.loaded = '1';
  } catch (e) {
    box.innerHTML = `<p class="small">Couldn't load the record: ${h(e.message)}</p>`;
  }
}

// ---------------------------------------------------------------- postings: everything ever collected

const PF = { q: '', status: 'all', region: 'all', active: 'all', sort: 'newest', page: 1 };
const PSTATUS = [['all', 'Any status'], ['listed', 'On your lists'], ['applied', 'Applied'], ['skipped', 'Skipped'],
  ['on_list', 'On your list now'], ['ruled_out', 'Ruled out'], ['released', 'Swapped out'], ['not_picked', 'Never picked'], ['closed', 'Closed']];
let postingsSeq = 0;

function postingsShell() {
  const sel = (id, opts, v) => `<select id="${id}">${opts.map(([k, l]) => `<option value="${k}" ${k === v ? 'selected' : ''}>${h(l)}</option>`).join('')}</select>`;
  return `<div class="page-head"><h1>Postings</h1><p>Every posting JobApps has collected, and what happened to each one.</p></div>
    <div class="psummary" id="pf-summary"></div>
    <div class="filters">
      <input id="pf-q" type="search" placeholder="Search company, role or city" value="${h(PF.q)}" aria-label="Search postings">
      ${sel('pf-status', PSTATUS, PF.status)}
      ${sel('pf-region', [['all', 'Anywhere'], ['canada', 'In Canada'], ['other', 'Outside Canada']], PF.region)}
      ${sel('pf-active', [['all', 'Open and closed'], ['open', 'Still open'], ['closed', 'No longer listed']], PF.active)}
      ${sel('pf-sort', [['newest', 'Newest first'], ['posted', 'Recently posted'], ['activity', 'Your recent activity'], ['company', 'Company A to Z']], PF.sort)}
      <a class="btn small" id="pf-csv" href="#" download>Download CSV</a>
    </div>
    <div id="pf-list" class="plist" aria-live="polite"></div>
    <div class="pager"><button class="btn small" id="pf-prev">‹ Previous</button><span id="pf-range"></span><button class="btn small" id="pf-next">Next ›</button></div>`;
}

async function loadPostings() {
  const box = $('#tab-postings');
  if (!$('#pf-q')) {
    box.innerHTML = postingsShell();
    $('#pf-q').addEventListener('input', () => { clearTimeout(PF.timer); PF.timer = setTimeout(() => { PF.q = $('#pf-q').value; PF.page = 1; loadPostings(); }, 250); });
    for (const k of ['status', 'region', 'active', 'sort']) {
      $(`#pf-${k}`).addEventListener('change', () => { PF[k] = $(`#pf-${k}`).value; PF.page = 1; loadPostings(); });
    }
    $('#pf-prev').addEventListener('click', () => { PF.page = Math.max(1, PF.page - 1); loadPostings(); });
    $('#pf-next').addEventListener('click', () => { PF.page += 1; loadPostings(); });
  }
  const { timer, ...params } = PF;
  const qs = new URLSearchParams(params).toString();
  $('#pf-csv').href = `/api/postings.csv?${qs}`;
  const seq = ++postingsSeq;
  let d;
  try { d = await api(`/api/postings?${qs}`); } catch (e) {
    $('#pf-list').innerHTML = `<div class="empty"><h2>Couldn't load postings</h2><p>${h(e.message)}</p></div>`;
    return;
  }
  if (seq !== postingsSeq) return;  // you've typed more since
  const sm = d.summary;
  const chip = (status, n, label) => `<button class="pcount ${PF.status === status ? 'on' : ''}" data-pf-status="${status}"><b>${n.toLocaleString()}</b> ${label}</button>`;
  $('#pf-summary').innerHTML = [chip('all', sm.total, 'collected'), chip('listed', sm.ever_listed, 'on your lists'),
    chip('applied', sm.applied, 'applied'), chip('skipped', sm.skipped, 'skipped'), chip('ruled_out', sm.ruled_out, 'ruled out')].join('')
    + `<span class="small">${sm.active.toLocaleString()} still open, ${sm.canada.toLocaleString()} in Canada</span>`;
  $('#pf-list').innerHTML = d.rows.length ? d.rows.map(postingHTML).join('') : '<div class="empty"><h2>No postings match</h2><p>Try fewer words or another filter.</p></div>';
  const from = d.total ? (d.page - 1) * d.size + 1 : 0;
  $('#pf-range').textContent = `${from.toLocaleString()} to ${Math.min(d.total, d.page * d.size).toLocaleString()} of ${d.total.toLocaleString()}`;
  $('#pf-prev').disabled = d.page <= 1;
  $('#pf-next').disabled = d.page * d.size >= d.total;
}

function postingHTML(r) {
  const places = r.locations.slice(0, 2).join(' / ') + (r.locations.length > 2 ? ` +${r.locations.length - 2}` : '');
  const meta = [places, BOARD[r.ats] || r.ats, r.first_seen ? `seen ${shortDate(r.first_seen)}` : '', r.active ? '' : 'no longer listed'].filter(Boolean);
  const when = r.done_at ? shortDate(r.done_at) : r.listed_on ? `listed ${shortDate(r.listed_on)}` : '';
  const sub = [when, r.minutes ? `${r.minutes} min` : '', r.how === 'added' ? 'added by you' : ''].filter(Boolean).join(', ');
  return `<div class="prow">
    <div class="pmain"><a class="co" href="${h(r.url)}" target="_blank" rel="noopener">${h(r.company)}</a><span class="ti">${h(r.title)}</span>
      <span class="meta">${meta.map((m) => `<span>${h(m)}</span>`).join('')}</span></div>
    <div class="pstat"><span class="pchip ${h(r.status)}">${h(r.status_label)}</span>${sub ? `<small>${h(sub)}</small>` : ''}
      ${r.reason ? `<small class="why">${h(r.reason)}</small>` : ''}</div>
    <div class="pact">${r.can_add ? `<button class="btn small" data-add="${h(r.id)}">Add to today</button>` : ''}</div>
  </div>`;
}

// ---------------------------------------------------------------- events

// What you type in an answer is held in `unsaved` until the server has it, so a redraw, a refresh of the list or
// a failed save can't lose it: it is shown from here, sent again every few seconds, and marked "Not saved yet".
const SAVE_RETRY_MS = 5000;
const saveTimers = {};

function questionOf(job, qkey) {
  const j = STATE && STATE.jobs.find((x) => x.id === job);
  return j && j.questions.find((x) => x.qkey === qkey);
}

function keepUnsaved() {
  for (const u of Object.values(unsaved)) {
    const q = questionOf(u.job, u.qkey);
    if (q) { q.answer = u.text; q.edited = true; }
  }
}

// The server said no (the question left this job): trying again won't help, so the text stays here marked as such.
const unsavedNote = (u) => (u && u.refused ? `Not saved: ${u.refused}` : 'Not saved yet. Trying again…');

function dropUnsaved(k) {
  clearTimeout(saveTimers[k]);
  delete unsaved[k];
}

function saveAnswer(ta, wait = 600) {
  const k = `${ta.dataset.job}|${ta.dataset.qkey}`;
  const u = unsaved[k] || (unsaved[k] = { job: ta.dataset.job, qkey: ta.dataset.qkey });
  u.text = ta.value;
  keepUnsaved();
  clearTimeout(saveTimers[k]);
  saveTimers[k] = setTimeout(() => sendAnswer(k), wait);
}

async function sendAnswer(k) {
  clearTimeout(saveTimers[k]);
  const u = unsaved[k];
  if (!u || u.sending) return;  // the request in flight sends the rest when it comes back
  const text = u.text;
  u.sending = true;
  let error = null;
  try {
    await post(`/api/job/${encodeURIComponent(u.job)}/answer`, { qkey: u.qkey, text });
  } catch (e) {
    error = e;
    if (!u.failed) toast(`Couldn't save your answer: ${e.message}`, 6000);
  }
  u.sending = false;
  if (unsaved[k] !== u) return;  // a redraft replaced the answer meanwhile
  u.failed = !!error;
  u.refused = error && error.status >= 400 && error.status < 500 ? error.message : '';
  if (!error && u.text === text) delete unsaved[k];
  else if (!u.refused || u.text !== text) saveTimers[k] = setTimeout(() => sendAnswer(k), error ? SAVE_RETRY_MS : 0);
  const flag = document.querySelector(`[data-unsaved="${CSS.escape(k)}"]`);
  if (flag) { flag.textContent = unsavedNote(u); flag.hidden = !error; }
}

document.addEventListener('input', (e) => {
  if (e.target.matches('textarea[data-ask]')) { asks[e.target.dataset.ask] = e.target.value; return; }
  const ta = e.target.closest('textarea[data-qkey]');
  if (!ta) return;
  const counter = document.querySelector(`[data-count="${ta.id}"]`);
  if (counter) counter.innerHTML = countText(ta.value, limitOf(ta.dataset.limit));
  saveAnswer(ta);
  const checks = document.querySelector(`[data-checks="${ta.id}"]`);
  const q = questionOf(ta.dataset.job, ta.dataset.qkey);
  if (checks && q) checks.innerHTML = checksHTML(q);
});

document.addEventListener('focusout', (e) => {
  const ta = e.target.closest && e.target.closest('textarea[data-qkey]');
  if (ta) sendAnswer(`${ta.dataset.job}|${ta.dataset.qkey}`);  // don't sit on the last words
  if (e.target.matches('#tab-today textarea') && pendingRender) setTimeout(render, 50);
});

// Closing the tab mid-sentence: send what's left without waiting for the reply.
window.addEventListener('pagehide', () => {
  for (const u of Object.values(unsaved)) {
    fetch(`/api/job/${encodeURIComponent(u.job)}/answer`, { method: 'POST', keepalive: true,
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ qkey: u.qkey, text: u.text }) }).catch(() => {});
  }
});

document.addEventListener('toggle', (e) => {
  const d = e.target;
  if (d.matches && d.matches('details[data-key]')) openState[d.dataset.key] = d.open;
}, true);

function goDay(day) {
  viewDay = day && day !== (STATE && STATE.today) ? day : null;
  if ($('#tab-today').hidden) setTab('today');
  loadState();
  window.scrollTo({ top: 0 });
}

function nextPending(after) {
  const jobs = STATE.jobs;
  const i = jobs.findIndex((j) => j.id === after);
  return [...jobs.slice(i + 1), ...jobs.slice(0, i)].find((j) => j.status === 'pending');
}

document.addEventListener('click', async (e) => {
  const t = e.target.closest('button, a');
  if (!t) return;
  if (t.dataset.tab) {
    if (t.dataset.tab === 'today' && viewDay) goDay(null);
    return setTab(t.dataset.tab);
  }
  if (t.dataset.tabGo) return setTab(t.dataset.tabGo);
  if (t.dataset.day) return goDay(t.dataset.day);
  if (t.dataset.run) { e.preventDefault(); return startRun(t.dataset.run, Number(t.dataset.n) || 0); }
  if (t.dataset.opened) {  // the clock starts when you open the application (the extension keeps it running)
    try { navigator.sendBeacon(`/api/job/${encodeURIComponent(t.dataset.opened)}/opened`); } catch (err) { /* timing only */ }
  }
  if (t.dataset.toggle) {
    openJob = openJob === t.dataset.toggle ? null : t.dataset.toggle;
    render();
    const el = document.getElementById(`job-${openJob}`);
    if (el) el.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    return;
  }
  if (t.dataset.copy) {
    const el = document.getElementById(t.dataset.copy);
    const text = el.value !== undefined ? el.value : el.textContent;
    try { await navigator.clipboard.writeText(text); } catch {
      const tmp = document.createElement('textarea'); tmp.value = text; document.body.append(tmp); tmp.select(); document.execCommand('copy'); tmp.remove();
    }
    t.textContent = 'Copied';
    setTimeout(() => { t.textContent = 'Copy'; }, 1200);
    return;
  }
  if (t.dataset.status) {
    const id = t.dataset.id;
    const job = STATE.jobs.find((j) => j.id === id);
    await post(`/api/job/${encodeURIComponent(id)}/status`, { status: t.dataset.status });
    if (job) job.status = t.dataset.status;
    STATE.done = STATE.jobs.filter((j) => j.status !== 'pending').length;
    if (t.dataset.status !== 'pending' && openJob === id) openJob = (nextPending(id) || {}).id || null;
    if (t.dataset.status === 'pending') openJob = id;
    render();
    toast({ applied: 'Marked applied', skipped: 'Skipped', pending: 'Back on your list' }[t.dataset.status]);
    return;
  }
  if (t.dataset.redraftOne) return redraftOne(t);
  if (t.dataset.answer || t.dataset.dismiss || t.dataset.relearn || t.dataset.forget) return memoryAction(t);
  if (t.dataset.goto) { scrollTo = t.dataset.goto; return setTab('setup'); }
  if (t.dataset.record) return toggleRecord(t);
  if (t.dataset.pfStatus) { PF.status = t.dataset.pfStatus; PF.page = 1; $('#pf-status').value = PF.status; return loadPostings(); }
  if (t.dataset.add) {
    const res = await post(`/api/postings/${encodeURIComponent(t.dataset.add)}/add`);
    if (!res.started) { toast('Busy right now; try again in a moment'); return; }
    t.disabled = true;
    t.textContent = 'Adding…';
    toast('Adding it to today: checking it\'s open and drafting answers');
    startPolling();
    return;
  }
  if (t.dataset.restore) {
    const ta = document.getElementById(t.dataset.restore);
    const k = `${ta.dataset.job}|${ta.dataset.qkey}`;
    if (previous[k] == null) return;
    ta.value = previous[k];
    delete previous[k];
    saveAnswer(ta, 0);
    render();
    toast('Previous version put back');
    return;
  }
  if (t.dataset.redraft) {
    const res = await post(`/api/job/${encodeURIComponent(t.dataset.redraft)}/redraft`);
    if (!res.started) { toast('Busy right now; try again in a moment'); return; }
    Object.keys(unsaved).filter((k) => unsaved[k].job === t.dataset.redraft).forEach(dropUnsaved);  // replaced by the new drafts
    toast('Redrafting from your resume…');
    startPolling();
  }
});

// Coming back from an application tab: the extension may have marked it applied.
window.addEventListener('focus', () => { if (!pollTimer && !$('#tab-today').hidden) loadState(); });

setTab((location.hash || '#today').slice(1) || 'today');
loadState();
