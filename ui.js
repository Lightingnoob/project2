/* =====================================================================
 * ui.js — TransferPath SEP Planner (UI layer)
 *
 * DATA:  everything comes from the FastAPI backend through api.js
 *        (institutions, majors, course search, AP evaluation, GE options, generateSep, dev injector).
 *        Plans, term names ("Fall 2026") and critical paths are all computed
 *        server-side by sep_engine; this file only renders them as semester
 *        cards. The response's `graph` (Cytoscape nodes/edges) is not used.
 *        Plans depend on College + University + Major + GE pathway, plus the
 *        student's completed courses, starting semester and summer choice.
 * ===================================================================== */

/* ---------------------------------------------------------------------
 * 1. CONFIG + STATE
 * ------------------------------------------------------------------- */
// API campus systems -> dropdown section headers (same labels as the <optgroup>s in index.html)
const SYSTEM_LABELS = { UC: 'UC Campuses', CSU: 'CSU Campuses' };
const SEASONS = ['Spring', 'Summer', 'Fall'];   // calendar order within a year
const SEASON_ICON = { Fall: 'fa-leaf', Spring: 'fa-seedling', Summer: 'fa-sun' };
// Keyed by plan.kind: one "recommended" plan (normal workload) or "fast" + "balanced" tracks (heavy workload)
const PLAN_ICON = {
  recommended: { icon: 'fa-route', cls: 'bg-blue-100 text-blue-600 dark:bg-blue-500/15 dark:text-blue-300' },
  fast: { icon: 'fa-bolt', cls: 'bg-amber-100 text-amber-600 dark:bg-amber-500/15 dark:text-amber-300' },
  balanced: { icon: 'fa-scale-balanced', cls: 'bg-emerald-100 text-emerald-600 dark:bg-emerald-500/15 dark:text-emerald-300' }
};
const GE_NOTES = {
  'CAL-GETC': 'California General Education Transfer Curriculum — 34 units across Areas 1–6, accepted by both UC and CSU.',
  '7-Course Pattern': 'Campus breadth pattern — Reading & Composition (A and B) plus one course in each of the 7 breadth areas.'
};

const state = {
  institutions: { colleges: [], universities: [], majors: [] },   // each: [{ name, group }] (+ id, system, has_articulation)
  majorsFor: null,        // 'college|university' the major list below belongs to
  majors: null,           // [{ major, degree, label, academic_year, concentration }] from GET /majors (null = not loaded)
  courseInfo: {},         // code -> { title, units, known } from the college catalog (GET /courses/resolve)
  courseResults: [],      // autocomplete matches for the completed-courses input (GET /courses/search)
  completed: [],          // canonical catalog codes
  apScores: [],           // [{ subject: 'AP Calculus BC', score: 5 }] -> sent as `ap_scores`
  apInfo: {},             // subject -> AP evaluation (POST /ap/evaluate), shown compactly under the AP tags
  tagUniversity: null,    // TAG campus id from GET /tag/campuses, e.g. 'riverside' -> `tag_university` (null = None)
  tagCampuses: [],        // [{ id, name, entry_terms }] for the TAG University dropdown (from the TAG dataset)
  tagStatus: null,        // { key, status, explanation } TAG check of the form's major at the TAG campus (see refreshTagStatus)
  tagPanel: null,         // index into response.tag_evaluations while the TAG panel is open
  pathway: 'CAL-GETC',
  startTerm: null,        // e.g. 'Fall 2026'
  includeSummer: false,
  loading: false,
  response: null,         // SEPResponse from POST /generate-sep (shown on the result page)
  lastRequest: null,      // the request that produced `response`; Generate reuses it when the form is unchanged
  plannerScroll: 0,       // form page scroll position, restored when coming back from the result page
  plans: {},              // id -> SemesterPlan
  panel: null,            // { planId, slot, allOpen } while the GE panel is open (slot: see geSlotsOf)
  golden: null,           // dev injector status when mock data is loaded
  requestId: 0
};

const $ = id => document.getElementById(id);
const sum = arr => arr.reduce((s, x) => s + x, 0);
const plural = (n, w) => `${n} ${w}${n === 1 ? '' : 's'}`;
const esc = s => String(s).replace(/[&<>"']/g,
  ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));

/* ---------------------------------------------------------------------
 * 2. THEME
 * ------------------------------------------------------------------- */
function toggleTheme() {
  const dark = document.documentElement.classList.toggle('dark');
  try { localStorage.setItem('theme', dark ? 'dark' : 'light'); } catch (e) {}
  document.dispatchEvent(new CustomEvent('themechange', { detail: { dark } }));
}

$('themeToggle').addEventListener('click', toggleTheme);

/* ---------------------------------------------------------------------
 * 3. INPUTS: college / target / major search, completed-course tags,
 *    GE pathway, starting semester, summer toggle, dev injector
 * ------------------------------------------------------------------- */
const OPTION_CLS = 'menu-option px-3 py-2 cursor-pointer text-sm';
const GROUP_CLS = 'sticky top-0 px-3 py-1.5 text-[11px] font-semibold uppercase tracking-wider ' +
  'bg-slate-50 dark:bg-slate-900 text-slate-500 dark:text-slate-400 border-b border-slate-200 dark:border-slate-700 cursor-default';

/** Searchable dropdown. `groupOf(option)` adds a header row whenever the group changes. `filter: false` shows
 *  getOptions() as is (the options are already a server-side search result). */
function wireDropdown(input, list, getOptions, onPick, { describe = () => '', groupOf = null, limit = 8, filter = true } = {}) {
  const render = () => {
    const q = input.value.trim().toLowerCase();
    const opts = getOptions().filter(o => !filter || o.toLowerCase().includes(q)).slice(0, limit);
    let lastGroup = null;
    list.innerHTML = opts.map(o => {
      const g = groupOf ? groupOf(o) : null;
      const head = g && g !== lastGroup ? `<li role="presentation" class="${GROUP_CLS}">${esc(g)}</li>` : '';
      lastGroup = g;
      const d = describe(o);
      return head + `<li class="${OPTION_CLS}" data-v="${esc(o)}"><span class="font-medium">${esc(o)}</span>` +
        (d ? `<span class="ml-2 text-slate-400">${esc(d)}</span>` : '') + '</li>';
    }).join('');
    list.classList.toggle('hidden', !opts.length || document.activeElement !== input);
  };
  input.addEventListener('input', render);
  input.addEventListener('focus', render);
  input.addEventListener('blur', () => setTimeout(() => list.classList.add('hidden'), 150));
  list.addEventListener('mousedown', e => {
    const li = e.target.closest('li');
    if (!li) return;
    e.preventDefault();                       // keep focus; group headers are not pickable
    if (li.dataset.v !== undefined) { onPick(li.dataset.v); render(); }
  });
  return render;
}

function pickInstitution(inputId, value) {
  $(inputId).value = value;
  $(inputId).blur();
  onInstitutionChange();
}

/* Dropdown options come from the loaded data (GET /institutions, GET /majors). The hidden <select>s in
 * index.html only name the default college, and fill the lists while the API is unreachable. */
const optionKey = s => s.normalize('NFD').replace(/[̀-ͯ]/g, '').trim().toLowerCase();   // "San José" == "San Jose"

function readOptions(selectId) {
  return [...$(selectId).options].map(o => ({
    name: o.value.trim(),
    group: o.parentElement.tagName === 'OPTGROUP' ? o.parentElement.label : null,
    isDefault: o.defaultSelected
  }));
}

const BUILT_IN_OPTIONS = {
  colleges: readOptions('collegeOptions'),
  universities: readOptions('targetOptions')
};

/** Union by name (API entries win: they carry ids and data flags), each group's entries kept together. */
function mergeOptions(...lists) {
  const seen = new Set();
  const items = lists.flat().filter(o => {
    const key = optionKey(o.name);
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
  const groups = [...new Set(items.map(o => o.group))];
  return groups.flatMap(g => items.filter(o => o.group === g));
}

/** api = GET /institutions payload, or null when the API is unreachable. */
function setInstitutions(api) {
  const colleges = (api?.colleges || []).map(c => ({ ...c, group: null }));
  const universities = (api?.universities || []).map(u => ({ ...u, group: SYSTEM_LABELS[u.system] || 'Other' }));
  state.institutions.colleges = api ? colleges : BUILT_IN_OPTIONS.colleges;
  state.institutions.universities = api ? mergeOptions(universities) : BUILT_IN_OPTIONS.universities;
}

/** Prefill an empty college field with the built-in `selected` option (e.g. Chabot College). */
function applyDefaultOptions() {
  const def = BUILT_IN_OPTIONS.colleges.find(o => o.isDefault);
  if (def && !$('collegeInput').value.trim()) $('collegeInput').value = def.name;
  onInstitutionChange();
}

const optionNames = key => () => state.institutions[key].map(o => o.name);
const findOption = (key, name) => state.institutions[key].find(o => optionKey(o.name) === optionKey(name || ''));
const groupOfUniversity = name => findOption('universities', name)?.group ?? null;

setInstitutions(null);

wireDropdown($('collegeInput'), $('collegeList'), optionNames('colleges'),
  v => pickInstitution('collegeInput', v),
  { describe: n => (findOption('colleges', n)?.has_articulation === false ? 'no transfer pathways yet' : '') });
wireDropdown($('targetInput'), $('targetList'), optionNames('universities'),
  v => pickInstitution('targetInput', v),
  { groupOf: groupOfUniversity, limit: Infinity,
    describe: n => (findOption('universities', n)?.has_articulation === false ? 'no transfer data yet' : '') });
/* Intended major: only majors with articulation data for the chosen college + university (GET /majors) */
wireDropdown($('majorInput'), $('majorList'), () => (state.majors || []).map(m => m.label),
  v => pickInstitution('majorInput', v),
  { limit: Infinity, describe: label => {
      const m = (state.majors || []).find(x => x.label === label);
      return m ? [m.concentration && `${m.concentration} concentration`, m.academic_year].filter(Boolean).join(' · ') : '';
    } });

['collegeInput', 'targetInput', 'majorInput'].forEach(id => {
  $(id).addEventListener('input', updateGenerateEnabled);
  $(id).addEventListener('change', onInstitutionChange);
});

function onInstitutionChange() {
  updateGenerateEnabled();
  loadMajors();
  revalidateCourses();
  refreshApInfo();
  renderGeChoice();
  refreshTagStatus();
}

$('majorInput').addEventListener('input', renderTagHint);    // hide the TAG status of the previous major while typing

function updateGenerateEnabled() {
  $('generateBtn').disabled = state.loading || !state.startTerm ||
    ['collegeInput', 'targetInput', 'majorInput'].some(id => !$(id).value.trim());
}

/** Majors with articulation data for this college + university; the hint under the field says what's loaded. */
async function loadMajors() {
  const college = $('collegeInput').value.trim();
  const university = $('targetInput').value.trim();
  const key = `${college}|${university}`;
  if (key === state.majorsFor) return;
  state.majorsFor = key;
  state.majors = null;
  let data = null;
  if (college && university) {
    try { data = await fetchMajors(college, university); } catch (e) { console.error(e); }
  }
  if (state.majorsFor !== key) return;                      // superseded by a newer choice
  state.majors = data ? data.majors : null;
  renderMajorHint();
}

function renderMajorHint() {
  const hint = $('majorHint');
  const n = state.majors ? state.majors.length : null;
  hint.textContent = n === null ? 'Majors with transfer data appear after you pick a college and university'
    : n ? `${plural(n, 'major')} with articulation data for this college and university`
    : 'No transfer pathways are loaded for this college and university yet';
}

/* Completed courses: autocomplete from the college catalog (GET /courses/search), stored as canonical codes
 * (GET /courses/resolve maps 'mth1' -> 'MTH 1' and former codes like 'ENGL 1' -> 'ENGL C1000'). */
const courseInput = $('courseInput');
const renderCourseList = wireDropdown(courseInput, $('courseList'),
  () => state.courseResults.map(c => c.code).filter(c => !state.completed.includes(c)),
  addTag,
  { filter: false, limit: 12,
    describe: code => { const c = state.courseResults.find(x => x.code === code); return c ? c.title : ''; } });

let searchTimer = null;
let searchSeq = 0;
courseInput.addEventListener('input', () => {
  clearTimeout(searchTimer);
  const text = courseInput.value.trim();
  if (!text) { state.courseResults = []; renderCourseList(); return; }
  searchTimer = setTimeout(async () => {
    const seq = ++searchSeq;
    let results = [];
    try { results = (await searchCourses($('collegeInput').value.trim(), text)) || []; } catch (e) { console.error(e); }
    if (seq !== searchSeq) return;                          // a newer keystroke's search wins
    state.courseResults = results;
    results.forEach(c => (state.courseInfo[c.code] = { ...c, known: true }));
    renderCourseList();
  }, 150);
});

function normalizeCode(raw) {
  const s = raw.trim().toUpperCase().replace(/\s+/g, ' ');
  return s.replace(/^([A-Z]+)\s*(\d+[A-Z]*)$/, '$1 $2');   // "cs1" -> "CS 1"
}

/** Canonical catalog code for what was typed; unknown codes are kept (the plan notes say they weren't counted). */
async function canonicalCourse(code) {
  const college = $('collegeInput').value.trim();
  if (!college) return { code, known: null };
  try {
    const hit = await resolveCourse(college, code);
    return hit ? { ...hit, known: true } : { code, known: false };
  } catch (e) {
    return { code, known: null };                           // API unreachable: can't check
  }
}

async function addTag(raw) {
  let code = normalizeCode(raw);
  courseInput.value = '';
  state.courseResults = [];
  if (!code || state.completed.includes(code)) return;
  const info = state.courseInfo[code] || await canonicalCourse(code);
  code = info.code || code;
  state.courseInfo[code] = info;
  if (state.completed.includes(code)) { renderTags(); return; }
  state.completed.push(code);
  renderTags();
}

function removeTag(code) {
  state.completed = state.completed.filter(c => c !== code);
  renderTags();
}

/** A new college means a different catalog: check the tags again. */
let validatedFor = null;
async function revalidateCourses() {
  const college = $('collegeInput').value.trim();
  if (college === validatedFor) return;
  validatedFor = college;
  state.courseInfo = {};
  const results = await Promise.all(state.completed.map(canonicalCourse));
  if (validatedFor !== college) return;
  state.completed = [...new Set(results.map((r, i) => r.code || state.completed[i]))];
  results.forEach(r => (state.courseInfo[r.code] = r));
  renderTags();
}

function renderTags() {
  $('tagBox').querySelectorAll('.tag').forEach(t => t.remove());
  state.completed.forEach(code => {
    const info = state.courseInfo[code];
    const known = info && info.known;
    const style = !info || info.known === null
      ? 'bg-slate-100 text-slate-700 dark:bg-slate-700 dark:text-slate-200 ring-slate-200 dark:ring-slate-600'
      : known
        ? 'bg-blue-100 text-blue-800 dark:bg-blue-500/15 dark:text-blue-300 ring-blue-200 dark:ring-blue-500/30'
        : 'bg-amber-100 text-amber-800 dark:bg-amber-500/15 dark:text-amber-300 ring-amber-200 dark:ring-amber-500/30';
    const tag = document.createElement('span');
    tag.className = `tag inline-flex items-center gap-1.5 pl-2.5 pr-1.5 py-1 rounded-md text-sm font-medium ring-1 ${style}`;
    tag.title = known ? `${info.title}${info.units ? ` · ${info.units} units` : ''}`
      : info && info.known === false ? `Not in ${$('collegeInput').value.trim()}'s catalog` : '';
    tag.innerHTML = `${esc(code)}<button type="button" aria-label="Remove ${esc(code)}"
      class="w-4 h-4 grid place-items-center rounded hover:bg-black/10 dark:hover:bg-white/10">
      <i class="fa-solid fa-xmark text-[10px]"></i></button>`;
    tag.querySelector('button').addEventListener('click', e => { e.stopPropagation(); removeTag(code); });
    $('tagBox').insertBefore(tag, courseInput);
  });
}

courseInput.addEventListener('keydown', e => {
  if ((e.key === 'Enter' || e.key === ',') && courseInput.value.trim()) {
    e.preventDefault(); addTag(courseInput.value).then(renderCourseList);
  } else if (e.key === 'Backspace' && !courseInput.value && state.completed.length) {
    removeTag(state.completed[state.completed.length - 1]);
  }
});
$('tagBox').addEventListener('click', () => courseInput.focus());

/* AP scores: subject + score dropdowns, removable tags. One entry per subject (re-adding updates the score).
 * Subjects come from the loaded AP data (GET /ap/exams). Each exam is evaluated by the backend (POST /ap/evaluate):
 * the college's own chart (course waiver, GE areas) and the target campus's chart (reported only) stay separate. */
const apSubject = $('apSubjectSelect');
const apScore = $('apScoreSelect');

function updateApAddEnabled() {
  $('apAddBtn').disabled = !apSubject.value || !apScore.value;
}

function addApScore() {
  const subject = apSubject.value;
  const score = Number(apScore.value);
  if (!subject || !score) return;
  const existing = state.apScores.find(a => a.subject === subject);
  if (existing) existing.score = score;
  else state.apScores.push({ subject, score });
  apSubject.value = '';
  apScore.value = '';
  updateApAddEnabled();
  renderApTags();
  refreshApInfo({ force: true });
  apSubject.focus();
}

function removeApScore(subject) {
  state.apScores = state.apScores.filter(a => a.subject !== subject);
  delete state.apInfo[subject];
  renderApTags();
}

let apInfoKey = null;
/** Evaluate every AP entry for the chosen college + target (on add, and when either changes). */
async function refreshApInfo({ force = false } = {}) {
  const college = $('collegeInput').value.trim();
  const university = $('targetInput').value.trim();
  const key = `${college}|${university}|${JSON.stringify(state.apScores)}`;
  if (!college || !state.apScores.length || (!force && key === apInfoKey)) { renderApTags(); return; }
  apInfoKey = key;
  let data = null;
  try {
    data = await evaluateAp({ college, university: university || null, ap_scores: state.apScores });
  } catch (e) {
    console.error('AP evaluation unavailable:', e);
  }
  if (apInfoKey !== key || !data) return;
  state.apInfo = {};
  data.evaluations.forEach(ev => (state.apInfo[ev.subject] = ev));
  renderApTags();
}

/** One compact line per exam: college effect · GE effect · target campus (details in the tooltip). */
function apSummary(ev) {
  if (!ev) return '';
  if (ev.status === 'unknown_exam') return 'not in the loaded AP list';
  const cc = ev.community_college_effect || {};
  const parts = [];
  const college = $('collegeInput').value.trim().replace(/ College$/, '') || 'College';
  if (cc.status === 'applied') parts.push(`${college}: ${cc.waived_courses.join(', ')}`);
  else if (cc.status === 'choose_one') parts.push(`${college}: ${cc.listed_courses.join(' or ')}`);
  else if (cc.status === 'below_minimum') parts.push(`below ${college}'s minimum score (${cc.minimum_score})`);
  else if (cc.status === 'not_listed') parts.push(`not in ${college}'s AP chart`);
  else if (cc.status === 'no_course_waiver') parts.push(`${college}: no course`);
  const ge = ev.ge_effect || {};
  if (ge.status === 'listed' && ge.areas_text) parts.push(`GE ${ge.areas_text.split(';')[0]}`);
  const t = ev.target_campus_effect || {};
  if (t.campus) {
    const name = t.campus.replace(/^California State University, /, 'CSU ');
    parts.push(t.status === 'not_listed' || t.status === 'no_rule_for_score' ? `${name}: not listed`
      : t.status === 'not_loaded' ? '' : `${name}: needs review`);
  }
  return parts.filter(Boolean).join(' · ');
}

function apTooltip(ev) {
  if (!ev || ev.status !== 'evaluated') return '';
  const t = ev.target_campus_effect || {};
  const rules = (t.rules || []).map(r => `${r.context === 'campus_chart' ? '' : `[${r.context}] `}${r.course_credit || 'credit'}` +
    (r.ge_or_other_requirements ? ` (GE ${r.ge_or_other_requirements})` : '')).join('; ');
  return [ev.community_college_effect?.message, ...(ev.community_college_effect?.conditions || []),
          rules && `${t.campus}: ${rules} (the campus decides; not applied to your plan)`].filter(Boolean).join('\n');
}

function renderApTags() {
  $('apTags').innerHTML = state.apScores.map(a => `
    <span title="${esc(apTooltip(state.apInfo[a.subject]))}" class="inline-flex items-center gap-1.5 pl-2.5 pr-1.5 py-1 rounded-md text-sm font-medium ring-1 bg-indigo-50 text-indigo-800 ring-indigo-200 dark:bg-indigo-500/15 dark:text-indigo-300 dark:ring-indigo-500/30">
      ${esc(a.subject)} <span class="text-indigo-500 dark:text-indigo-400">(${a.score})</span>
      <button type="button" data-remove-ap="${esc(a.subject)}" aria-label="Remove ${esc(a.subject)}"
        class="w-4 h-4 grid place-items-center rounded hover:bg-black/10 dark:hover:bg-white/10">
        <i class="fa-solid fa-xmark text-[10px]"></i></button>
    </span>`).join('');
  $('apDetails').innerHTML = state.apScores.map(a => {
    const line = apSummary(state.apInfo[a.subject]);
    return line ? `<li class="truncate" title="${esc(apTooltip(state.apInfo[a.subject]))}"><span class="font-medium text-slate-600 dark:text-slate-300">${esc(a.subject.replace(/^AP /, ''))}</span> → ${esc(line)}</li>` : '';
  }).join('');
}

/** AP subjects from the loaded AP data; the built-in options stay if the API is unreachable. */
async function loadApExams() {
  try {
    const { exams } = await fetchApExams();
    if (!exams.length) return;
    apSubject.innerHTML = '<option value="">Select AP subject…</option>' +
      exams.map(e => `<option value="${esc(e.name)}">${esc(e.name)}</option>`).join('');
  } catch (e) {
    console.error('AP subjects unavailable:', e);
  }
}

apSubject.addEventListener('change', updateApAddEnabled);
apScore.addEventListener('change', updateApAddEnabled);
$('apAddBtn').addEventListener('click', addApScore);
$('apTags').addEventListener('click', e => {
  const b = e.target.closest('[data-remove-ap]');
  if (b) removeApScore(b.dataset.removeAp);
});

// GE pathway, TAG campus, start term and summer are read by Generate (no live regeneration: results are
// on their own page)
document.querySelectorAll('input[name="gePathway"]').forEach(r => r.addEventListener('change', () => {
  state.pathway = r.value;
}));

/* The UC 7-course pattern is a UC admission requirement, not GE: offered only when the target is a UC. */
function renderGeChoice() {
  const system = findOption('universities', $('targetInput').value.trim())?.system;
  const sevenOk = system !== 'CSU';                          // unknown target: leave the choice to the backend
  const seven = document.querySelector('input[name="gePathway"][value="7-Course Pattern"]');
  seven.disabled = !sevenOk;
  seven.closest('label').classList.toggle('opacity-50', !sevenOk);
  seven.closest('label').classList.toggle('cursor-not-allowed', !sevenOk);
  if (!sevenOk && state.pathway === '7-Course Pattern') {
    state.pathway = 'CAL-GETC';
    document.querySelector('input[name="gePathway"][value="CAL-GETC"]').checked = true;
  }
  $('geHint').textContent = sevenOk
    ? 'GE shows up in each semester as GE rows you can open.'
    : 'The UC 7-course pattern is a UC admission requirement, so it applies to UC targets only.';
}

/* Starting semester: the next 7 terms from today (Summer terms only when summer is on) */
const termOrdinal = label => { const [s, y] = label.split(' '); return Number(y) * 3 + SEASONS.indexOf(s); };

function termOptions(includeSummer) {
  const now = new Date();
  const m = now.getMonth();                                   // Jan–May Spring, Jun–Jul Summer, Aug–Dec Fall
  let ord = now.getFullYear() * 3 + (m <= 4 ? 0 : m <= 6 ? 1 : 2);
  const out = [];
  for (; out.length < 7; ord++) {
    const season = SEASONS[ord % 3];
    if (includeSummer || season !== 'Summer') out.push(`${season} ${Math.floor(ord / 3)}`);
  }
  return out;
}

function renderStartTerms() {
  const opts = termOptions(state.includeSummer);
  const prev = state.startTerm;
  state.startTerm = !prev ? opts[0]
    : opts.includes(prev) ? prev
    : opts.find(o => termOrdinal(o) >= termOrdinal(prev)) || opts[0];   // e.g. Summer 2027 -> Fall 2027
  $('startTermSelect').innerHTML = opts.map(o =>
    `<option value="${esc(o)}"${o === state.startTerm ? ' selected' : ''}>${esc(o)}</option>`).join('');
  updateGenerateEnabled();
}

$('tagSelect').addEventListener('change', e => {
  state.tagUniversity = e.target.value || null;
  refreshTagStatus();
});

/* TAG University options come from the TAG dataset (GET /tag/campuses). Values are campus ids; a request
 * saved before ids were used may hold a name ('UC Davis'), which is matched by name. */
function renderTagOptions() {
  const wanted = state.tagUniversity;
  const match = wanted && state.tagCampuses.find(c => c.id === wanted || c.name.toLowerCase() === String(wanted).toLowerCase());
  if (match) state.tagUniversity = match.id;
  else if (wanted && state.tagCampuses.length) state.tagUniversity = null;     // no longer a TAG campus
  $('tagSelect').innerHTML = '<option value="">None</option>' +
    state.tagCampuses.map(c => `<option value="${esc(c.id)}">${esc(c.name)}</option>`).join('');
  $('tagSelect').value = match ? match.id : '';
  refreshTagStatus();
}

/* TAG status under the TAG University field, as soon as a major and a TAG campus are chosen (no plan needed).
 * POST /tag/evaluate checks the major against the campus's exclusions for the Fall entry term its loaded rules
 * cover (plans end in Spring, for Fall admission); the tag_engine resolves the major's school/college. Only
 * this major check is shown here. The result page evaluates every TAG requirement for the plan's transfer term. */
const TAG_MAJOR_STATUS = {
  eligible: { label: 'TAG available for this major', icon: 'fa-circle-check', tone: 'ok' },
  ineligible: { label: 'TAG unavailable for this major', icon: 'fa-triangle-exclamation', tone: 'warn' },
  needs_review: { label: 'TAG eligibility needs review', icon: 'fa-circle-exclamation', tone: 'review' }
};
const TAG_HINT_CLS = 'mt-1.5 text-xs text-slate-500 dark:text-slate-400';

/** The campus, entry term and major the TAG status is for; null until both a campus and a major are chosen. */
function tagStatusQuery() {
  const campus = state.tagCampuses.find(c => c.id === state.tagUniversity);
  const term = campus && (campus.entry_terms.find(t => t.key.startsWith('fall_')) || campus.entry_terms[0]);
  const major = $('majorInput').value.trim();
  return campus && term && major ? { campus, term, major, key: `${campus.id}|${term.key}|${major}` } : null;
}

/** Status from the evaluation's major-exclusion check (null when it has none, e.g. no TAG for that term). */
function tagMajorStatus(evaluation) {
  const check = evaluation.checks.find(c => c.id === 'campus.major_exclusion');
  if (!check) return null;
  const status = check.status === 'met' ? 'eligible' : check.status === 'not_met' ? 'ineligible' : 'needs_review';
  return { status, explanation: check.explanation };
}

async function refreshTagStatus() {
  const q = tagStatusQuery();
  if (q && state.tagStatus?.key !== q.key) {
    state.tagStatus = { key: q.key };                  // pending: the generic hint shows meanwhile
    renderTagHint();
    let status = null;
    try {
      status = tagMajorStatus(await evaluateTag({
        campus: q.campus.id, entry_term: q.term.key, student: { intended_major: q.major } }));
    } catch (e) {
      console.error('TAG check unavailable:', e);      // the generic hint stays; the next change retries
    }
    if (state.tagStatus?.key !== q.key) return;        // superseded by a newer major or campus
    state.tagStatus = status && { key: q.key, ...status };
  }
  renderTagHint();
}

function renderTagHint() {
  const hint = $('tagHint');
  const q = tagStatusQuery();
  const s = state.tagStatus;
  const look = q && s && s.key === q.key && TAG_MAJOR_STATUS[s.status];
  if (look) {
    const tone = TAG_TONES[look.tone];
    hint.className = `mt-1.5 rounded-lg border ${tone.box} px-2.5 py-1.5 text-xs`;
    hint.innerHTML = `
      <p class="flex items-center gap-1.5 font-semibold"><i class="fa-solid ${look.icon}" aria-hidden="true"></i>${look.label}</p>
      <p class="mt-0.5 leading-snug ${tone.sub}">${esc(s.explanation)}</p>`;
    return;
  }
  const c = state.tagCampuses.find(x => x.id === state.tagUniversity);
  hint.className = TAG_HINT_CLS;
  hint.textContent = c
    ? `Rules loaded for ${c.entry_terms.map(t => t.label).join(' and ')} entry · checked against your plan's transfer term`
    : state.tagCampuses.length ? `Transfer Admission Guarantee · ${state.tagCampuses.length} participating UCs`
    : 'Transfer Admission Guarantee';
}

async function loadTagCampuses() {
  try {
    state.tagCampuses = (await fetchTagCampuses()).campuses;
  } catch (e) {
    console.error('TAG campuses unavailable:', e);      // the planner still works without TAG
  }
  renderTagOptions();
}

$('startTermSelect').addEventListener('change', e => {
  state.startTerm = e.target.value;
});
$('summerToggle').addEventListener('change', e => {
  state.includeSummer = e.target.checked;
  renderStartTerms();
});

/** Put a saved request back into the form (page refresh). The text fields + state are the form's state. */
function applyRequestToForm(r) {
  $('collegeInput').value = r.college;
  $('targetInput').value = r.university;
  $('majorInput').value = r.major;
  state.completed = [...r.completed_courses];
  state.apScores = r.ap_scores.map(({ subject, score }) => ({ subject, score }));
  state.tagUniversity = r.tag_university;
  renderTagOptions();
  state.pathway = r.ge_pathway;
  document.querySelectorAll('input[name="gePathway"]').forEach(el => (el.checked = el.value === r.ge_pathway));
  state.includeSummer = r.include_summer;
  $('summerToggle').checked = r.include_summer;
  state.startTerm = r.start_term;           // renderStartTerms() keeps it if it is still offered
}

/* Backend connection banner */
function setApiStatus(error) {
  const box = $('apiStatus');
  box.classList.toggle('hidden', !error);
  if (!error) return;
  // offline = no response at all (server not running); otherwise the server answered with an error
  const hint = error.offline
    ? `Start it with <code class="px-1.5 py-0.5 rounded bg-amber-100 dark:bg-amber-500/20 font-mono text-xs">uvicorn main:app --reload</code>`
    : 'Check the uvicorn terminal for the traceback, then retry.';
  box.innerHTML = `<div class="flex flex-wrap items-center gap-x-3 gap-y-2">
    <i class="fa-solid ${error.offline ? 'fa-plug-circle-xmark' : 'fa-bug'}"></i>
    <span class="flex-1 min-w-[200px]">${esc(error.message)} ${hint}</span>
    <button type="button" id="apiRetry" class="px-2.5 py-1 rounded-md ring-1 ring-amber-300 dark:ring-amber-500/40 hover:bg-amber-100 dark:hover:bg-amber-500/20 font-medium">Retry</button>
  </div>`;
  $('apiRetry').addEventListener('click', init);
}

/* Dev-only: hackathon data injector (loads the Golden Pathway into the backend) */
const DEV_BTN = 'inline-flex items-center gap-1.5 px-2 py-1 rounded-md border border-dashed border-slate-300 dark:border-slate-700 ' +
  'text-slate-600 dark:text-slate-300 hover:border-slate-400 hover:text-slate-700 dark:hover:border-slate-500 dark:hover:text-slate-200';

function renderDevTools() {
  const g = state.golden;
  $('devTools').innerHTML = !g
    ? `<button type="button" data-dev="inject" class="${DEV_BTN}" title="Loads one demo pathway into the backend">
         <i class="fa-solid fa-flask"></i> Developer: Inject Mock Data</button>`
    : `<span class="inline-flex items-center gap-1.5 text-emerald-700 dark:text-emerald-300" title="Only this pathway has data">
         <i class="fa-solid fa-circle-check"></i> Mock data: ${esc(g.pathway.college)} → ${esc(g.pathway.university)} → ${esc(g.pathway.major)}</span>
       <button type="button" data-dev="fill" class="${DEV_BTN}"><i class="fa-solid fa-pen-to-square"></i> Fill form</button>
       <button type="button" data-dev="clear" class="${DEV_BTN}"><i class="fa-solid fa-trash-can"></i> Clear</button>`;
}

$('devTools').addEventListener('click', async e => {
  const b = e.target.closest('[data-dev]');
  if (!b) return;
  if (b.dataset.dev === 'fill') {
    const { pathway, suggested_completed } = state.golden;
    $('collegeInput').value = pathway.college;
    $('targetInput').value = pathway.university;
    $('majorInput').value = pathway.major;
    state.completed = [...suggested_completed];
    renderTags();
    onInstitutionChange();
    return;
  }
  try {
    const status = b.dataset.dev === 'inject' ? await injectMockData() : await clearMockData();
    state.golden = status.loaded ? status : null;
    setApiStatus(null);
  } catch (err) {
    if (err.offline) setApiStatus(err); else console.error(err);
    return;
  }
  resetResults();               // shown results were computed against the old data set
  state.majorsFor = null;       // the mock pathway adds a major: reload the list
  loadMajors();
  renderDevTools();
});

async function init() {
  try {
    setInstitutions(await fetchInstitutions());
    state.majorsFor = null;                    // lists now come from the API: refresh what depends on them
    onInstitutionChange();
    loadApExams();
    await loadTagCampuses();
    const status = await fetchMockDataStatus();
    state.golden = status.loaded ? status : null;
    setApiStatus(null);
  } catch (e) {
    console.error(e);
    setApiStatus(e.offline ? e
      : new Error(e.status ? `The TransferPath API returned HTTP ${e.status}.` : 'The TransferPath API returned an error.'));
  }
  renderDevTools();
}

/* ---------------------------------------------------------------------
 * 4. RESULTS: POST /generate-sep -> result page, or an N/A notice under the form
 * ------------------------------------------------------------------- */
$('generateBtn').addEventListener('click', () => generatePlans());

const GENERATE_LABEL = $('generateBtn').innerHTML;
function setLoading(on) {
  state.loading = on;
  $('generateBtn').innerHTML = on
    ? '<i class="fa-solid fa-circle-notch fa-spin"></i> Building your plans…'
    : GENERATE_LABEL;
  $('resultView').classList.toggle('opacity-60', on);
  $('resultView').classList.toggle('pointer-events-none', on);
  updateGenerateEnabled();
}

function buildRequest() {
  return {
    college: $('collegeInput').value.trim(),
    university: $('targetInput').value.trim(),
    major: $('majorInput').value.trim(),
    ge_pathway: state.pathway,
    completed_courses: [...state.completed],
    ap_scores: state.apScores.map(({ subject, score }) => ({ subject, score })),
    tag_university: state.tagUniversity,
    start_term: state.startTerm,
    include_summer: state.includeSummer
  };
}

const sameRequest = (a, b) => !!a && !!b && JSON.stringify(a) === JSON.stringify(b);

/**
 * Generate button: build plans for the form and open the result page. N/A and errors stay on the form page.
 * refresh: re-run in place on the result page after a GE check-off — keeps the GE panel open.
 */
async function generatePlans({ refresh = false } = {}) {
  const request = buildRequest();
  if (!request.college || !request.university || !request.major || !request.start_term) return;

  // Form unchanged since the plan on hand was built: show it again without another API call
  if (!refresh && state.response && sameRequest(request, state.lastRequest)) return openResultPage();

  const req = ++state.requestId;
  setLoading(true);
  let data = null, error = null;
  try {
    data = await generateSep(request);                        // <- BACKEND (api.js -> POST /generate-sep)
  } catch (e) {
    error = e;
    console.error(e);
  }
  if (req !== state.requestId) return;                        // superseded by a newer request
  setLoading(false);
  setApiStatus(error && error.offline ? error : null);

  if (!data) {
    resetResults();
    renderUnavailable(request, error);
    if (currentView() === 'result') {                          // a check-off re-run failed: back to the form
      history.replaceState(null, '', ROUTES.planner);
      showRoute();
    }
    $('plannerNotice').scrollIntoView({ behavior: 'smooth', block: 'center' });
    return;
  }

  const panel = state.panel;
  $('plannerNotice').innerHTML = '';
  $('resultView').classList.toggle('no-reveal', refresh);     // entrance animation only for a new plan
  renderPlans(data, request);
  savePlan(request, data);
  if (refresh && panel && state.plans[panel.planId]) { state.panel = panel; renderGePanel(); }
  else closeGePanel();
  if (refresh && state.tagPanel !== null) renderTagPanel();
  else closeTagPanel();
  if (!refresh) openResultPage();
}

/** Forget the current plan (new data set, or generation failed) — in memory and in sessionStorage. */
function resetResults() {
  closeTermFocus();
  closeGePanel();
  closeTagPanel();
  state.plans = {};
  state.response = null;
  state.lastRequest = null;
  $('resultsNotes').innerHTML = '';
  $('resultsBody').innerHTML = '';
  $('exportPdfStatus').textContent = '';
  savePlan(null);
}

function toggleCompleted(code) {
  state.completed = state.completed.includes(code)
    ? state.completed.filter(c => c !== code)
    : [...state.completed, code];
  state.courseInfo[code] ??= { code, known: true };          // GE options are catalog codes already
  renderTags();
  generatePlans({ refresh: true });
}

function renderUnavailable(request, error) {
  const step = (icon, label, value) => `
    <div class="flex items-center gap-2.5 rounded-lg bg-slate-50 dark:bg-slate-800/60 ring-1 ring-slate-200 dark:ring-slate-700 px-3 py-2 text-left min-w-0">
      <i class="fa-solid ${icon} text-slate-400 dark:text-slate-500"></i>
      <div class="min-w-0">
        <div class="text-[10px] uppercase tracking-wider text-slate-400">${label}</div>
        <div class="text-sm font-medium text-slate-700 dark:text-slate-200 truncate">${esc(value)}</div>
      </div>
    </div>`;
  const arrow = '<i class="fa-solid fa-arrow-right text-slate-300 dark:text-slate-600 rotate-90 sm:rotate-0"></i>';

  if (error) {
    const title = error.offline ? 'Couldn’t reach the server' : 'Couldn’t build a plan';
    $('plannerNotice').innerHTML = `
    <div class="card fade-in surface surface-dashed rounded-xl border-2 border-dashed px-6 py-12 text-center">
      <div class="mx-auto w-14 h-14 rounded-full grid place-items-center bg-slate-100 dark:bg-slate-800 text-slate-400 dark:text-slate-500">
        <i class="fa-solid ${error.offline ? 'fa-plug-circle-xmark' : 'fa-triangle-exclamation'} text-2xl"></i>
      </div>
      <h2 class="mt-4 text-xl font-semibold">${title}</h2>
      <p class="mt-2 text-sm text-slate-500 dark:text-slate-400 max-w-md mx-auto">${esc(error.message)}</p>
    </div>`;
    return;
  }

  $('plannerNotice').innerHTML = `
  <div class="card fade-in surface surface-dashed rounded-xl border-2 border-dashed px-6 py-12 sm:py-14 text-center">
    <div class="relative mx-auto w-16 h-16">
      <div class="absolute inset-0 rounded-2xl bg-blue-500/10 dark:bg-blue-400/10 rotate-6"></div>
      <div class="relative w-16 h-16 rounded-2xl grid place-items-center bg-white dark:bg-slate-800 ring-1 ring-slate-200 dark:ring-slate-700 text-slate-400 dark:text-slate-500 shadow-sm">
        <i class="fa-solid fa-folder-open text-2xl"></i>
      </div>
    </div>
    <h2 class="mt-5 text-xl font-semibold tracking-tight">N/A - Transfer data for this specific pathway has not been uploaded yet.</h2>
    <p class="mt-2 text-sm text-slate-500 dark:text-slate-400 max-w-lg mx-auto">
      SEP plans are generated only from uploaded articulation agreements for this exact
      college, university and major combination.</p>
    <div class="mt-6 flex flex-col sm:flex-row items-stretch sm:items-center justify-center gap-2 sm:gap-3 max-w-3xl mx-auto">
      ${step('fa-school', 'Community college', request.college)}
      ${arrow}
      ${step('fa-building-columns', 'Target university', request.university)}
      ${arrow}
      ${step('fa-graduation-cap', 'Intended major', request.major)}
    </div>
    <p class="mt-4 text-xs text-slate-400">GE pathway: ${esc(request.ge_pathway)} ·
      Starting ${esc(request.start_term)} · Summer ${request.include_summer ? 'on' : 'off'}</p>
  </div>`;
}

/**
 * Fill the result page: summary, workload, TAG status and collapsed plan notes in #resultsNotes, plan cards in #resultsBody. `request` produced `resp`.
 * Only `semesters`, `summary`, `requirements` and `critical_path` are read; `graph` is ignored.
 */
function renderPlans(resp, request) {
  state.response = resp;
  state.lastRequest = request;
  state.plans = {};
  resp.plans.forEach(p => (state.plans[p.id] = p));
  $('exportPdfStatus').textContent = '';

  const pw = resp.pathway;
  const first = resp.plans[0];
  const reqs = first.requirements;
  const majorReqs = reqs.filter(r => r.category === 'major');
  const majorCourses = majorReqs.flatMap(r => r.courses);
  const majorDone = majorCourses.filter(c => c.status === 'completed').length;
  // GE: the plan's `ge` block lists every requirement of the pattern (real data); mock data only has groups
  const geRows = first.ge ? first.ge.requirements : reqs.filter(r => r.category === 'ge');
  const geDone = first.ge
    ? geRows.filter(r => ['completed', 'ap', 'major_prep'].includes(r.status)).length
    : geRows.filter(r => r.courses.every(c => c.status === 'completed')).length;
  const geText = pw.ge_pathway === '7-Course Pattern' ? 'requirements' : 'areas';

  $('resultsNotes').innerHTML = `
    <div class="fade-in rounded-xl border border-blue-200 dark:border-blue-900/60 bg-blue-50 dark:bg-blue-950/40 text-blue-900 dark:text-blue-100 px-4 py-2.5 flex gap-3">
      <i class="fa-solid fa-circle-info mt-0.5"></i>
      <div class="text-sm leading-relaxed">
        <p>
        Planning <b>${esc(pw.degree)}</b> at <b>${esc(pw.university)}</b> from <b>${esc(pw.college)}</b>
        using <b>${esc(pw.ge_pathway)}</b>, starting <b>${esc(pw.start_term)}</b>${pw.include_summer ? ' with summer classes' : ''}.
        Major prep: <b>${majorDone}/${majorCourses.length}</b> done ·
        GE: <b>${geDone}/${geRows.length}</b> ${geText} covered${first.ge ? ' (completed, AP or major prep)' : ''}.
        Every plan ends in a Spring term, ready for Fall transfer admission.
        Click a teal <b>GE</b> row to see the courses that fill it.
        </p>
        ${dataSourcesHTML(resp)}
      </div>
    </div>
    ${workloadHTML(resp.workload)}
    ${(resp.tag_evaluations || []).map((entry, i) => tagBannerHTML(entry, i, resp)).join('')}
    ${planNotesHTML(resp.warnings)}`;
  closeTermFocus();
  $('resultsBody').innerHTML = resp.plans.map(cardHTML).join('');
}

/**
 * TAG, as evaluated by the backend's tag_engine for each plan's transfer term (one banner per term):
 *   eligible        green   every requirement appears met (never worded as "guaranteed admission")
 *   ineligible      amber   a requirement is definitively not met, e.g. the major is excluded for that term
 *   needs_review    blue    nothing is known to fail, but something is unknown or needs verification
 *   not_applicable  gray    not a TAG campus, or no TAG for that entry term
 * The UI only renders the evaluation; TAG rules live in data/uc_tag_requirements_*.json and tag_engine/.
 */
const TAG_TONES = {
  ok: { box: 'border-emerald-200 dark:border-emerald-500/30 bg-emerald-50 dark:bg-emerald-500/10 text-emerald-900 dark:text-emerald-100',
        icon: 'bg-emerald-100 text-emerald-600 dark:bg-emerald-500/20 dark:text-emerald-300', sub: 'text-emerald-800/80 dark:text-emerald-200/80',
        chip: 'ring-emerald-200 dark:ring-emerald-500/30' },
  warn: { box: 'border-amber-200 dark:border-amber-500/30 bg-amber-50 dark:bg-amber-500/10 text-amber-900 dark:text-amber-100',
          icon: 'bg-amber-100 text-amber-600 dark:bg-amber-500/20 dark:text-amber-300', sub: 'text-amber-800/90 dark:text-amber-200/80',
          chip: 'ring-amber-200 dark:ring-amber-500/30' },
  review: { box: 'border-blue-200 dark:border-blue-500/30 bg-blue-50 dark:bg-blue-500/10 text-blue-900 dark:text-blue-100',
            icon: 'bg-blue-100 text-blue-600 dark:bg-blue-500/20 dark:text-blue-300', sub: 'text-blue-800/80 dark:text-blue-200/80',
            chip: 'ring-blue-200 dark:ring-blue-500/30' },
  neutral: { box: 'surface', icon: 'bg-slate-100 text-slate-500 dark:bg-slate-800 dark:text-slate-400',
             sub: 'text-slate-600 dark:text-slate-300', chip: 'ring-slate-200 dark:ring-slate-700' }
};
const TAG_STATUS = {
  eligible: { label: 'Appears met', icon: 'fa-circle-check', tone: 'ok' },
  ineligible: { label: 'Not met', icon: 'fa-circle-xmark', tone: 'warn' },
  needs_review: { label: 'Needs review', icon: 'fa-circle-exclamation', tone: 'review' },
  not_applicable: { label: 'Not applicable', icon: 'fa-circle-minus', tone: 'neutral' }
};
const tagLook = status => {
  const look = TAG_STATUS[status] || TAG_STATUS.needs_review;
  return { ...look, tone: TAG_TONES[look.tone] };
};

/** 'Plan A & Plan B' when the response has more than one plan (otherwise the term says it all). */
function tagPlanNames(entry, resp = state.response) {
  if (!resp || resp.plans.length < 2) return '';
  return entry.plan_ids.map(id => (resp.plans.find(p => p.id === id) || {}).name).filter(Boolean).join(' & ');
}

function tagBannerHTML(entry, index, resp) {
  const t = entry.evaluation;
  const look = tagLook(t.status);
  const meta = [t.entry_term && `${t.entry_term.label} entry`, tagPlanNames(entry, resp), t.major].filter(Boolean).join(' · ');
  const details = t.checks.length || t.reference;
  return `
    <div class="fade-in flex flex-wrap sm:flex-nowrap items-start gap-3 rounded-xl border ${look.tone.box} p-4">
      <div class="w-9 h-9 shrink-0 rounded-lg grid place-items-center ${look.tone.icon}"><i class="fa-solid fa-handshake"></i></div>
      <div class="min-w-0 flex-1">
        <div class="flex flex-wrap items-center gap-x-2 gap-y-1">
          <span class="font-semibold">TAG · ${esc(t.campus ? t.campus.name : state.lastRequest?.tag_university || 'University')}</span>
          <span class="${CHIP} ${look.tone.chip} bg-white/60 dark:bg-white/5"><i class="fa-solid ${look.icon}"></i> ${look.label}</span>
          ${meta ? `<span class="text-xs ${look.tone.sub}">${esc(meta)}</span>` : ''}
        </div>
        <p class="mt-1 text-sm font-medium">${esc(t.headline)}</p>
        <p class="mt-0.5 text-sm ${look.tone.sub}">${esc(t.summary)}</p>
      </div>
      ${details ? `
      <button type="button" data-tag-open="${index}"
        class="shrink-0 self-center inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-sm font-medium ring-1 ${look.tone.chip} bg-white/70 dark:bg-white/5 hover:bg-white dark:hover:bg-white/10 focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500">
        TAG requirements <i class="fa-solid fa-chevron-right text-xs"></i>
      </button>` : ''}
    </div>`;
}

/**
 * The backend's plan warnings (articulation gaps, loader notes, GE not yet planned) as one collapsed,
 * low-emphasis disclosure under the TAG status, so they never compete with the semester plan.
 */
function planNotesHTML(notes) {
  if (!notes || !notes.length) return '';
  return `
    <details class="group/notes fade-in text-sm">
      <summary class="cursor-pointer list-none inline-flex items-center gap-1.5 text-xs font-medium text-slate-500 dark:text-slate-400 hover:text-slate-700 dark:hover:text-slate-200">
        <i class="fa-solid fa-circle-info"></i> Plan notes (${notes.length})
        <i class="fa-solid fa-chevron-down text-[10px] transition-transform group-open/notes:rotate-180"></i>
      </summary>
      <ul class="mt-2 space-y-1.5 rounded-lg ring-1 ring-slate-200 dark:ring-slate-700/70 px-4 py-3 text-slate-600 dark:text-slate-300">
        ${notes.map(n => `<li class="flex gap-2"><span class="text-slate-400 dark:text-slate-500">•</span><span>${esc(n)}</span></li>`).join('')}
      </ul>
    </details>`;
}

/** A subtle one-line record of which datasets and years the plan used (older years marked, never a banner). */
function dataSourcesHTML(resp) {
  const ds = resp.data_sources;
  if (!ds) return resp.articulation && resp.articulation.source === 'mock'
    ? '<p class="mt-1 text-xs opacity-75">Data: demo mock pathway (illustrative, not real articulation).</p>' : '';
  const yr = y => (y ? y.replace(/^(\d{4})-\d{2}(\d{2})$/, '$1–$2') : '');
  const item = (label, src) => src && src.status === 'loaded' && src.academic_year
    ? `${label} ${yr(src.academic_year)}${src.year_status === 'older_year' ? ' (latest loaded)' : ''}` : null;
  const parts = [
    item('ASSIST agreement', ds.articulation),
    item('catalog', ds.catalog),
    ds.ge && ds.ge.status === 'loaded' ? (item(`${resp.pathway.ge_pathway} list`, ds.ge) || `${resp.pathway.ge_pathway}`) : null,
    ds.prerequisites && ds.prerequisites.status === 'loaded' ? `prerequisites reviewed ${ds.prerequisites.reviewed_on}` : null
  ].filter(Boolean);
  return parts.length ? `<p class="mt-1 text-xs opacity-75">Data: ${esc(parts.join(' · '))}</p>` : '';
}

/** Why there are one or two plans (backend's workload analysis). */
function workloadHTML(w) {
  if (!w) return '';
  const heavy = w.classification === 'heavy';
  return `
    <div class="fade-in flex items-center gap-3 rounded-xl border surface px-4 py-2.5">
      <div class="w-8 h-8 shrink-0 rounded-lg grid place-items-center ${heavy
        ? 'bg-amber-100 text-amber-600 dark:bg-amber-500/15 dark:text-amber-300'
        : 'bg-blue-100 text-blue-600 dark:bg-blue-500/15 dark:text-blue-300'}">
        <i class="fa-solid ${heavy ? 'fa-weight-hanging' : 'fa-scale-balanced'}"></i></div>
      <div class="min-w-0 text-sm">
        <div class="font-semibold">${heavy ? 'Heavy workload' : 'Normal workload'} ·
          ${w.total_required_units} units to go · ${plural(w.plan_count, 'plan')}</div>
        <p class="mt-0.5 text-slate-600 dark:text-slate-300">${esc(w.explanation)}</p>
        ${w.notes.map(n => `<p class="mt-0.5 text-xs text-slate-500 dark:text-slate-400">${esc(n)}</p>`).join('')}
      </div>
    </div>`;
}

// Semester grid: 1 column on phones, 2 from md, all 4 semesters in one row from xl (5+ semesters wrap)
const SEMESTER_GRID = 'w-full grid grid-cols-1 md:grid-cols-2 xl:grid-cols-4 gap-6 2xl:gap-8';

/**
 * One plan: header with Terms / Units / Transfer, then its semester cards directly below.
 * On the desktop result page the card fills the remaining viewport height and the semester grid takes
 * everything under the header (flex-1), so the semester cards stretch to the bottom of the screen.
 */
function cardHTML(p) {
  const s = p.summary;
  const style = PLAN_ICON[p.kind] || PLAN_ICON.recommended;
  const stat = (label, value) => `
    <div class="sm:text-right">
      <div class="text-xs uppercase tracking-wide text-slate-500 dark:text-slate-400">${label}</div>
      <div class="text-xl font-bold whitespace-nowrap">${esc(value)}</div>
    </div>`;

  return `
  <article class="plan-enter flex-1 flex flex-col w-full card surface rounded-xl border overflow-hidden">
    <div class="plan-header flex flex-wrap items-center gap-x-4 gap-y-3 px-5 py-4 sm:px-6">
      <div class="w-11 h-11 shrink-0 rounded-lg grid place-items-center text-lg ${style.cls}">
        <i class="fa-solid ${style.icon}"></i>
      </div>
      <div class="flex-1 min-w-0">
        <h3 class="text-lg font-semibold">${esc(p.name)} <span class="text-slate-400 dark:text-slate-500 font-normal">·</span> ${esc(p.label)}</h3>
        <p class="text-sm text-slate-500 dark:text-slate-400">${esc(p.description)}</p>
      </div>
      <div class="flex w-full sm:w-auto gap-8">
        ${stat('Terms', s.terms_needed)}
        ${stat('Units', s.total_units)}
        ${stat('Transfer', s.transfer_admission_term || '—')}
      </div>
    </div>

    <div class="flex-1 flex flex-col gap-5 border-t border-slate-200 dark:border-slate-800 p-5 sm:p-6">
      ${s.major_prep_in_summer.length ? `
      <div class="plan-note rounded-lg bg-amber-50 dark:bg-amber-500/10 ring-1 ring-amber-200 dark:ring-amber-500/30 px-3 py-2 text-sm text-amber-900 dark:text-amber-200">
        <i class="fa-solid fa-sun mr-1.5"></i>Major prep in summer (needed to finish on time): <b>${s.major_prep_in_summer.map(esc).join(', ')}</b>
      </div>` : ''}

      <div class="plan-semesters ${SEMESTER_GRID} flex-1">
        ${p.semesters.length ? p.semesters.map((sem, i) => semesterCardHTML(p, sem, i === p.semesters.length - 1)).join('')
          : `<div class="col-span-full text-sm text-emerald-600 dark:text-emerald-400"><i class="fa-solid fa-circle-check mr-1"></i>All requirements complete — you're ready to transfer!</div>`}
      </div>
    </div>
  </article>`;
}

/**
 * The GE slots of one semester. The backend returns each one typed (`type: 'ge_slot'`) with its real requirement
 * in `ge` ({ pathway, requirement_id, name, lab_required, institution_id, ... }); the semester card shows just
 * "GE · units", and the GE panel asks the backend for the courses that fill it (api.js fetchGeOptions).
 *   { type: 'ge', areaId: '5B', areaName: 'Cal-GETC 5B: Biological Sciences', units: 3, term: 'Fall 2027', ge }
 * Mock data has no `ge` block: its GE courses are matched to their requirement group instead.
 */
function geSlotsOf(p, sem) {
  const geReqs = p.requirements.filter(r => r.category === 'ge');
  return sem.courses.filter(c => c.is_ge).map(c => {
    const req = geReqs.find(r => r.courses.some(x => x.code === c.code));   // a course fills at most one GE area
    if (c.ge) {
      return { type: 'ge', code: c.code, areaId: req ? req.id : c.ge.slot_id, requirementId: c.ge.requirement_id,
               areaName: `${c.ge.display_name} ${c.ge.requirement_id}: ${c.ge.name}${c.ge.lab_required ? ' (with lab)' : ''}`,
               units: c.units, term: sem.term, ge: c.ge, startTerm: state.response?.pathway.start_term };
    }
    return { type: 'ge', code: c.code, areaId: req ? req.id : null, areaName: req ? req.name : c.satisfies[0] || 'General Education',
             units: c.units, term: sem.term };
  });
}

/** A GE slot in the semester's course list: teal, "GE", units and a chevron. Details open in the GE panel. */
function geSlotRowHTML(p, sem, slot, index) {
  const ring = sem.season === 'Summer' ? 'ring-amber-300 dark:ring-amber-500/40' : 'ring-teal-200 dark:ring-teal-500/30';
  // -mx-3 lets the tint bleed into the card padding so "GE" lines up with the course codes above
  return `
        <li>
          <button type="button" data-ge-slot="${index}" data-plan="${p.id}" data-sem="${sem.index}" data-area="${esc(slot.areaId || '')}"
            aria-label="GE requirement, ${plural(slot.units, 'unit')}. Show GE details"
            class="-mx-3 w-[calc(100%+1.5rem)] flex items-center gap-3 rounded-lg px-3 py-2 text-left bg-teal-50 dark:bg-teal-500/10 ring-1 ${ring} hover:bg-teal-100 dark:hover:bg-teal-500/20 transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-teal-500">
            <span class="flex-1 text-[15px] font-semibold text-teal-800 dark:text-teal-200"><i class="fa-solid fa-layer-group text-xs mr-2"></i>GE</span>
            <span class="text-xs tabular-nums text-teal-700/70 dark:text-teal-300/70">${plural(slot.units, 'unit')}</span>
            <i class="fa-solid fa-chevron-right text-xs text-teal-600/70 dark:text-teal-400/70"></i>
          </button>
        </li>`;
}

function semesterCardHTML(p, s, isLast) {
  const summer = s.season === 'Summer';
  const major = s.courses.filter(c => !c.is_ge);
  const frame = s.is_padding
    ? 'border-dashed border-emerald-300 dark:border-emerald-500/40 bg-emerald-50/60 dark:bg-emerald-500/5'
    : summer
      ? 'border-amber-200 dark:border-amber-500/30 bg-amber-50/70 dark:bg-amber-500/5'
      : 'surface-tile';
  const iconColor = { Fall: 'text-orange-500', Spring: 'text-emerald-500', Summer: 'text-amber-500' }[s.season];

  let body;
  if (s.is_padding) {
    body = `<p class="text-sm text-emerald-700 dark:text-emerald-300 leading-snug">
      <i class="fa-solid fa-graduation-cap mr-1"></i>Transfer-ready — no courses required. Ending on this Spring
      lines you up for <b>Fall ${s.year}</b> admission.</p>`;
  } else if (!s.courses.length) {
    body = '<p class="text-sm text-slate-400">No courses this term.</p>';
  } else {
    // Two lines per course: code + units, then the title on its own line so it gets the card's full width
    // A course needed only as another course's prerequisite gets a quiet "prerequisite" label (type from the API)
    const courseRow = c => `
        <li data-course="${esc(c.id)}">
          <div class="flex items-baseline justify-between gap-3">
            <span class="text-[15px] font-semibold whitespace-nowrap">${esc(c.code)}${c.type === 'prerequisite'
              ? ` <span class="ml-1 text-[11px] font-normal text-slate-400" title="${esc(c.satisfies.join('; '))}">prerequisite</span>` : ''}</span>
            <span class="shrink-0 text-xs tabular-nums text-slate-400">${plural(c.units, 'unit')}</span>
          </div>
          <div class="mt-0.5 text-[15px] leading-snug break-words text-slate-600 dark:text-slate-300">${esc(c.title)}</div>
        </li>`;
    body = `
      <ul class="space-y-3 xl:space-y-4">${major.map(courseRow).join('')}${geSlotsOf(p, s).map((slot, i) => geSlotRowHTML(p, s, slot, i)).join('')}
      </ul>
      ${isLast ? `
      <p class="mt-auto pt-4 text-xs text-emerald-600 dark:text-emerald-400"><i class="fa-solid fa-graduation-cap mr-1"></i>Transfer-ready for Fall ${s.year}</p>` : ''}`;
  }

  return `
    <div data-term-card data-plan="${p.id}" data-sem="${s.index}" class="min-w-0 rounded-xl border ${frame} p-5 2xl:p-6 flex flex-col">
      <div class="flex justify-between gap-2 text-[13px] font-semibold text-slate-500 dark:text-slate-400 mb-4">
        <span class="inline-flex items-center gap-1.5 tracking-wide"><i class="fa-solid ${SEASON_ICON[s.season]} ${iconColor}"></i>${esc(s.term.toUpperCase())}</span>
        <span class="whitespace-nowrap">${s.units}/${s.max_units} units</span>
      </div>
      ${body}
    </div>`;
}

// One delegated handler for every GE slot row: opens the GE panel on that slot's requirement
$('resultsBody').addEventListener('click', e => {
  const row = e.target.closest('[data-ge-slot]');
  if (!row) return;
  const plan = state.plans[row.dataset.plan];
  const slot = geSlotsOf(plan, plan.semesters[Number(row.dataset.sem) - 1])[Number(row.dataset.geSlot)];
  if (slot) openGePanel(plan.id, slot);
});

// Clicking a semester card (not its GE rows) glides it to the centre while the page fades to white
// (focus_card.js); once it lands, its courses turn into the plan's prerequisite map (plan_graph.js)
let termFocus = null;

$('resultsBody').addEventListener('click', e => {
  const card = e.target.closest('[data-term-card]');
  if (!card || e.target.closest('button, select, input, a, summary')) return;
  const plan = state.plans[card.dataset.plan];
  const graph = plan ? termGraph(plan, plan.semesters[Number(card.dataset.sem) - 1]) : null;
  termFocus = focusCard(card, graph ? { widthRatio: graph.widthRatio(card), prepare: graph.prepare,
                                         onOpen: graph.show, onCloseStart: graph.hide } : {});
});

/** Drop a focused card at once, before the cards under it are replaced or hidden. */
function closeTermFocus() {
  if (termFocus) termFocus.close({ instant: true });
  termFocus = null;
}

/* ---------------------------------------------------------------------
 * 5. GE REQUIREMENTS SIDE PANEL
 * ------------------------------------------------------------------- */
let panelReturnFocus = null;

/** slot: the GE slot whose row was clicked (fixed while the panel is open); none = all GE requirements */
function openGePanel(planId, slot = null) {
  state.panel = { planId, slot, allOpen: !slot };
  renderGePanel();
  panelReturnFocus = document.activeElement;
  $('gePanel').classList.add('open');
  $('gePanel').setAttribute('aria-hidden', 'false');
  document.body.style.overflow = 'hidden';
  setTimeout(() => $('geClose').focus(), 60);
}

function closeGePanel() {
  if (!state.panel) return;
  state.panel = null;
  $('gePanel').classList.remove('open');
  $('gePanel').setAttribute('aria-hidden', 'true');
  document.body.style.overflow = '';
  if (panelReturnFocus && panelReturnFocus.focus && document.contains(panelReturnFocus)) panelReturnFocus.focus();
}

$('geClose').addEventListener('click', closeGePanel);
$('geBackdrop').addEventListener('click', closeGePanel);
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeGePanel(); });

$('geBody').addEventListener('change', e => {
  const cb = e.target.closest('input[data-code]');
  if (cb && state.panel && !state.loading) toggleCompleted(cb.dataset.code);
});

const CHIP = 'inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[11px] font-medium ring-1';

/** Everything the panel knows about a course code in this plan (from its semesters, then its requirements). */
function courseInfo(p, code) {
  const reqNames = p.requirements.filter(r => r.courses.some(c => c.code === code)).map(r => r.name);
  for (const sem of p.semesters) {
    const c = sem.courses.find(x => x.code === code);
    if (c) return { label: c.code, title: c.title, status: 'planned', term: sem.term, is_ge: c.is_ge, units: c.units,
                    satisfies: c.satisfies.length ? c.satisfies : reqNames };
  }
  const c = p.requirements.flatMap(r => r.courses).find(x => x.code === code);
  if (c) return { label: c.code, title: c.title, status: c.status, term: c.term, is_ge: c.kind === 'ge', units: null, satisfies: reqNames };
  return { label: code, title: '', status: 'completed', term: null, is_ge: true, units: null, satisfies: [] };
}

function statusChip(info) {
  if (info.status === 'completed') return `<span class="${CHIP} bg-emerald-50 text-emerald-700 ring-emerald-200 dark:bg-emerald-500/10 dark:text-emerald-300 dark:ring-emerald-500/30">
    <i class="fa-solid fa-check"></i> Completed</span>`;
  if (!info.is_ge) return `<span class="${CHIP} bg-blue-50 text-blue-700 ring-blue-200 dark:bg-blue-500/10 dark:text-blue-300 dark:ring-blue-500/30">
    <i class="fa-solid fa-link"></i> Major prep · ${esc(info.term)}</span>`;
  const summer = String(info.term).startsWith('Summer');
  return `<span class="${CHIP} ${summer
    ? 'bg-amber-50 text-amber-700 ring-amber-200 dark:bg-amber-500/10 dark:text-amber-300 dark:ring-amber-500/30'
    : 'bg-teal-50 text-teal-700 ring-teal-200 dark:bg-teal-500/10 dark:text-teal-300 dark:ring-teal-500/30'}">
    <i class="fa-${summer ? 'solid fa-sun' : 'regular fa-clock'}"></i> Planned · ${esc(info.term)}</span>`;
}

function courseItemHTML(info) {
  const done = info.status === 'completed';
  const editable = info.is_ge;   // major prep is read-only here (use the Completed courses field)
  const control = editable
    ? `<input type="checkbox" data-code="${esc(info.label)}" ${done ? 'checked' : ''}
         class="mt-0.5 w-4 h-4 shrink-0 accent-teal-600 cursor-pointer" aria-label="Mark ${esc(info.label)} completed" />`
    : `<span class="mt-0.5 w-4 h-4 shrink-0 grid place-items-center text-[11px] ${done
        ? 'text-emerald-600 dark:text-emerald-400' : 'text-blue-600 dark:text-blue-400'}">
         <i class="fa-solid ${done ? 'fa-circle-check' : 'fa-link'}"></i></span>`;
  const Tag = editable ? 'label' : 'div';
  return `
  <${Tag} class="flex gap-3 rounded-lg border p-3 transition-colors ${done
    ? 'border-slate-200 dark:border-slate-800 bg-slate-50 dark:bg-slate-800/40'
    : 'border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900'} ${editable ? 'cursor-pointer hover:border-teal-400 dark:hover:border-teal-500/60' : ''}">
    ${control}
    <div class="flex-1 min-w-0">
      <div class="flex items-baseline justify-between gap-2">
        <div class="text-sm font-medium ${done ? 'text-slate-500 dark:text-slate-400 line-through decoration-slate-400/70' : ''}">
          ${esc(info.label)} <span class="font-normal text-slate-500 dark:text-slate-400">${esc(info.title)}</span></div>
        ${info.units ? `<span class="text-xs text-slate-500 dark:text-slate-400 shrink-0">${info.units}u</span>` : ''}
      </div>
      <div class="mt-1.5">${statusChip(info)}</div>
      ${info.satisfies.length ? `<div class="mt-2 flex flex-wrap gap-1">${info.satisfies.map(s =>
        `<span class="px-1.5 py-0.5 rounded text-[11px] bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-300">${esc(s)}</span>`).join('')}</div>` : ''}
    </div>
  </${Tag}>`;
}

const GE_STATE_CHIP = {
  done: ['Complete', 'bg-emerald-50 text-emerald-700 ring-emerald-200 dark:bg-emerald-500/10 dark:text-emerald-300 dark:ring-emerald-500/30'],
  ap: ['AP credit', 'bg-indigo-50 text-indigo-700 ring-indigo-200 dark:bg-indigo-500/10 dark:text-indigo-300 dark:ring-indigo-500/30'],
  major: ['Via major prep', 'bg-blue-50 text-blue-700 ring-blue-200 dark:bg-blue-500/10 dark:text-blue-300 dark:ring-blue-500/30'],
  pending: ['In plan', 'bg-teal-50 text-teal-700 ring-teal-200 dark:bg-teal-500/10 dark:text-teal-300 dark:ring-teal-500/30'],
  missing: ['Not planned', 'bg-slate-100 text-slate-600 ring-slate-200 dark:bg-slate-800 dark:text-slate-300 dark:ring-slate-700']
};
const stateChip = st => `<span class="${CHIP} ${GE_STATE_CHIP[st][1]}">${GE_STATE_CHIP[st][0]}</span>`;
/** Backend GE row status -> chip */
const geRowState = r => ({ completed: 'done', ap: 'ap', major_prep: 'major', planned: 'pending' })[r.status] || 'missing';

/** One GE requirement of the plan's `ge` block (real data): name, what satisfies it, when. */
function geRowHTML(r) {
  const st = geRowState(r);
  const detail = r.satisfied_by ? esc(r.satisfied_by.label)
    : r.status === 'planned' ? `GE slot · ${esc(r.term || '')}${r.lab_required ? ' · with lab' : ''}`
    : 'No eligible course could be planned; ask a counselor.';
  return `
    <div class="flex items-start justify-between gap-3 rounded-lg border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 p-3">
      <div class="min-w-0">
        <div class="text-sm font-medium">${esc(r.requirement_id)} · ${esc(r.name)}</div>
        <div class="mt-0.5 text-xs text-slate-500 dark:text-slate-400">${detail}${r.term && r.satisfied_by ? ` · ${esc(r.term)}` : ''}</div>
      </div>
      ${stateChip(st)}
    </div>`;
}

function renderGePanel() {
  const { planId, slot, allOpen } = state.panel;
  const p = state.plans[planId];
  if (!p) return closeGePanel();
  const pw = state.response.pathway;
  const real = !!p.ge;
  const geReqs = p.requirements.filter(r => r.category === 'ge');
  // Mock data: requirement groups with their courses; real data: the plan's `ge` block
  const reqState = r => r.courses.every(c => c.status === 'completed') ? 'done'
    : r.courses.every(c => c.status === 'completed' || c.kind === 'major') ? 'major' : 'pending';
  const rows = real ? p.ge.requirements : geReqs;
  const stateOf = r => (real ? geRowState(r) : reqState(r));
  const counts = { done: 0, ap: 0, major: 0, pending: 0, missing: 0 };
  rows.forEach(r => counts[stateOf(r)]++);
  const total = rows.length || 1;
  const pct = n => `${(n / total) * 100}%`;
  const pendingUnits = sum(p.semesters.flatMap(s => s.courses.filter(c => c.is_ge).map(c => c.units)));

  $('geTitle').textContent = slot ? 'GE requirement' : 'GE requirements';
  $('geSubtitle').textContent = slot ? `${slot.term} · ${plural(slot.units, 'unit')} · ${pw.ge_pathway}`
    : `${p.name} · ${pw.ge_pathway} · ${pw.university}`;

  // The clicked slot: its requirement (status from the latest plan), then the courses that can fill it
  const slotRow = slot && (real ? p.ge.requirements.find(r => slot.ge && r.slot_id === slot.ge.slot_id)
    : geReqs.find(r => r.id === slot.areaId));
  const slotHTML = !slot ? '' : `
    <section class="rounded-xl bg-teal-50 dark:bg-teal-500/10 ring-1 ring-teal-200 dark:ring-teal-500/30 p-4">
      <div class="text-xs font-semibold uppercase tracking-wider text-teal-700 dark:text-teal-300">Fills this requirement</div>
      <div class="mt-1 text-lg font-semibold leading-snug">${esc(slot.areaName)}</div>
      <div class="mt-2 flex flex-wrap items-center gap-1.5 text-xs text-slate-600 dark:text-slate-300">
        ${slotRow ? stateChip(real ? geRowState(slotRow) : reqState(slotRow)) : ''}
        <span>${real && slotRow && slotRow.status !== 'planned'
          ? `No longer a GE slot in this plan${slotRow.satisfied_by ? ` (${esc(slotRow.satisfied_by.label)})` : ''}`
          : `${plural(slot.units, 'unit')} planned for ${esc(slot.term)}`}</span>
      </div>
      <p id="geRule" class="mt-2 text-xs text-slate-600 dark:text-slate-300 empty:hidden">${slot.ge && slot.ge.rule ? esc(slot.ge.rule) : ''}</p>
    </section>
    <section>
      <h3 class="mb-2 text-xs font-semibold uppercase tracking-wider text-slate-500 dark:text-slate-400">Courses that fill it</h3>
      <div id="geOptions" aria-live="polite"></div>
    </section>`;

  const summary = real
    ? `${esc(p.ge.display_name)} from ${esc(pw.college)}'s ${esc(p.ge.academic_year || '')} list`
      + (p.ge.year_status === 'older_year' ? ` (the latest loaded; you start in ${esc(p.ge.requested_academic_year)})` : '') + '.'
    : esc(GE_NOTES[pw.ge_pathway] || pw.ge_pathway);
  const legend = [['bg-emerald-500', counts.done, 'complete'], ['bg-indigo-500', counts.ap, 'AP credit'],
                  ['bg-blue-500', counts.major, 'via major prep']].filter(([, n]) => n || !real)
    .map(([cls, n, label]) => `<span><span class="inline-block w-2 h-2 rounded-full ${cls} mr-1"></span>${n} ${label}</span>`).join('');

  $('geBody').innerHTML = `
    ${slotHTML}
    <details id="geAll" class="group" ${allOpen ? 'open' : ''}>
      <summary class="cursor-pointer list-none flex items-center justify-between text-xs font-semibold uppercase tracking-wider text-slate-500 dark:text-slate-400 hover:text-slate-700 dark:hover:text-slate-200">
        <span>All ${esc(pw.ge_pathway)} requirements</span>
        <i class="fa-solid fa-chevron-down transition-transform group-open:rotate-180"></i>
      </summary>
      <div class="mt-4 space-y-5">
        <section class="rounded-lg bg-slate-50 dark:bg-slate-800/50 ring-1 ring-slate-200 dark:ring-slate-800 p-4">
          <p class="text-sm text-slate-600 dark:text-slate-300">${summary}</p>
          <div class="mt-3 h-2 rounded-full bg-slate-200 dark:bg-slate-700 overflow-hidden flex">
            <div class="h-full bg-emerald-500" style="width:${pct(counts.done)}"></div>
            <div class="h-full bg-indigo-500" style="width:${pct(counts.ap)}"></div>
            <div class="h-full bg-blue-500" style="width:${pct(counts.major)}"></div>
          </div>
          <div class="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-slate-600 dark:text-slate-300">
            ${legend}
            <span><span class="inline-block w-2 h-2 rounded-full bg-slate-300 dark:bg-slate-600 mr-1"></span>${counts.pending} in plan · ${pendingUnits} GE units scheduled</span>
          </div>
          ${real && p.ge.lab ? `<p class="mt-2 text-xs text-slate-500 dark:text-slate-400">Lab (5C): ${esc(
            p.ge.lab.satisfied_by ? p.ge.lab.satisfied_by.label
              : p.ge.lab.status === 'major_prep' ? `through your ${p.ge.lab.via_requirement} major-prep course`
              : p.ge.lab.status === 'planned' ? `with the ${p.ge.lab.via_requirement} slot` : 'not planned')}</p>` : ''}
        </section>
        ${real ? `<div class="space-y-2">${rows.map(geRowHTML).join('')}</div>` : geReqs.map(r => `
        <div>
          <div class="flex items-center justify-between gap-2 mb-2">
            <h4 class="text-xs font-semibold text-slate-600 dark:text-slate-300">${esc(r.name)}${r.choose > 1 ? ` <span class="font-normal text-slate-400">(choose ${r.choose})</span>` : ''}</h4>
            ${stateChip(reqState(r))}
          </div>
          <div class="space-y-2">${r.courses.map(c => courseItemHTML(courseInfo(p, c.code))).join('')}</div>
        </div>`).join('')}
      </div>
    </details>`;
  if (slot) renderGeOptions(slot, pw);
  $('geAll').addEventListener('toggle', e => { if (state.panel) state.panel.allOpen = e.target.open; });
}

/** Courses that can fill a GE slot, from the college's GE list + catalog (api.js fetchGeOptions). Checking one
 *  marks it completed and re-plans (the slot then disappears). */
async function renderGeOptions(slot, pw) {
  const box = $('geOptions');
  const empty = (icon, title, note) => `
    <div class="rounded-lg border-2 border-dashed border-slate-200 dark:border-slate-700 px-4 py-6 text-center">
      <i class="fa-solid ${icon} text-xl text-slate-300 dark:text-slate-600"></i>
      <p class="mt-2 text-sm font-medium text-slate-600 dark:text-slate-300">${title}</p>
      <p class="mt-1 text-xs text-slate-500 dark:text-slate-400">${note}</p>
    </div>`;
  let data;
  try {
    data = await fetchGeOptions({ ...slot, pathway: pw.ge_pathway, college: pw.college });
  } catch (e) {
    console.error(e);
    data = e;
  }
  if (!state.panel || state.panel.slot !== slot || !document.contains(box)) return;   // panel moved on
  if (data instanceof Error) {
    box.innerHTML = empty('fa-triangle-exclamation', 'Couldn’t load GE course options.', esc(data.message));
    return;
  }
  if (!data) {
    box.innerHTML = empty('fa-list-check', 'No course list for this demo requirement.',
      'Demo mock data has no GE course list; real pathways list the college’s approved courses here.');
    return;
  }
  if (data.status !== 'loaded') {
    box.innerHTML = empty('fa-folder-open', 'No course list is loaded for this requirement.', esc(data.message || ''));
    return;
  }
  if (!data.options.length) {
    box.innerHTML = empty('fa-folder-open', 'No courses are listed for this requirement.', esc(slot.areaName));
    return;
  }
  const withPrereq = data.options.filter(o => o.prerequisite).length;
  const head = [
    data.year_status === 'older_year' ? `${data.institution} ${data.academic_year} list (the latest loaded)` : `${data.institution} ${data.academic_year} list`,
    data.lab_required ? 'showing courses that include the lab' : null,
    data.minimum_disciplines ? `the ${data.minimum_disciplines} courses must come from different disciplines` : null,
    withPrereq ? `${withPrereq} of ${data.options.length} list a prerequisite` : null
  ].filter(Boolean).join(' · ');
  box.innerHTML = `
    <p class="mb-2 text-xs text-slate-500 dark:text-slate-400">${esc(head)}</p>
    <ul class="space-y-2">${data.options.map(o => {
      const done = state.completed.includes(o.code);
      return `
      <li>
        <label class="flex gap-3 rounded-lg border p-3 cursor-pointer transition-colors hover:border-teal-400 dark:hover:border-teal-500/60 ${done
          ? 'border-slate-200 dark:border-slate-800 bg-slate-50 dark:bg-slate-800/40' : 'border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900'}">
          <input type="checkbox" data-code="${esc(o.code)}" ${done ? 'checked' : ''}
            class="mt-0.5 w-4 h-4 shrink-0 accent-teal-600 cursor-pointer" aria-label="Mark ${esc(o.code)} completed" />
          <div class="flex-1 min-w-0">
            <div class="flex items-baseline justify-between gap-3">
              <span class="text-sm font-semibold">${esc(o.code)}</span>
              ${o.units != null ? `<span class="shrink-0 text-xs tabular-nums text-slate-500 dark:text-slate-400">${plural(Number(o.units), 'unit')}</span>` : ''}
            </div>
            <div class="text-sm text-slate-600 dark:text-slate-300">${esc(o.title)}</div>
            ${o.prerequisite ? `<div class="mt-1 text-xs text-slate-500 dark:text-slate-400">Prerequisite: ${esc(o.prerequisite)}</div>` : ''}
            ${o.also_counts_for.length ? `<div class="mt-1 text-[11px] text-slate-400">Also listed for ${esc(o.also_counts_for.join(', '))} (counts toward one area)</div>` : ''}
          </div>
        </label>
      </li>`;
    }).join('')}</ul>`;
}

/* ---------------------------------------------------------------------
 * 5b. TAG REQUIREMENTS PANEL — one TAG evaluation from the API: its checks, the reference rules (when no
 *     rules are published for the plan's term) and the issues the TAG data flags. Renders only.
 * ------------------------------------------------------------------- */
const TAG_CHECK = {
  met: { icon: 'fa-circle-check', iconCls: 'text-emerald-600 dark:text-emerald-400', label: 'Met', labelCls: 'text-emerald-700 dark:text-emerald-300' },
  not_met: { icon: 'fa-circle-xmark', iconCls: 'text-amber-600 dark:text-amber-400', label: 'Not met', labelCls: 'text-amber-700 dark:text-amber-300' },
  unknown: { icon: 'fa-circle-question', iconCls: 'text-slate-400 dark:text-slate-500', label: 'Unknown', labelCls: 'text-slate-500 dark:text-slate-400' },
  manual_review: { icon: 'fa-circle-exclamation', iconCls: 'text-blue-600 dark:text-blue-400', label: 'Needs review', labelCls: 'text-blue-700 dark:text-blue-300' },
  info: { icon: 'fa-circle-info', iconCls: 'text-slate-400 dark:text-slate-500', label: '', labelCls: '' }
};
// Check groups in display order (null = "<campus> requirements"). Coverage is the status block + footer.
const TAG_CHECK_GROUPS = [['campus', null], ['shared', 'Every TAG campus requires'],
  ['eligibility', 'Eligibility to apply'], ['timeline', 'Dates and process']];
const safeUrl = u => (/^https?:\/\//.test(u || '') ? u : null);
let tagReturnFocus = null;

function openTagPanel(index) {
  state.tagPanel = index;
  renderTagPanel();
  tagReturnFocus = document.activeElement;
  $('tagPanel').classList.add('open');
  $('tagPanel').setAttribute('aria-hidden', 'false');
  document.body.style.overflow = 'hidden';
  setTimeout(() => $('tagClose').focus(), 60);
}

function closeTagPanel() {
  if (state.tagPanel === null) return;
  state.tagPanel = null;
  $('tagPanel').classList.remove('open');
  $('tagPanel').setAttribute('aria-hidden', 'true');
  document.body.style.overflow = '';
  if (tagReturnFocus && tagReturnFocus.focus && document.contains(tagReturnFocus)) tagReturnFocus.focus();
}

$('tagClose').addEventListener('click', closeTagPanel);
$('tagBackdrop').addEventListener('click', closeTagPanel);
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeTagPanel(); });
$('resultsNotes').addEventListener('click', e => {
  const b = e.target.closest('[data-tag-open]');
  if (b) openTagPanel(Number(b.dataset.tagOpen));
});

function renderTagPanel() {
  const entry = state.response && (state.response.tag_evaluations || [])[state.tagPanel];
  if (!entry) return closeTagPanel();
  const t = entry.evaluation;
  const look = tagLook(t.status);
  $('tagTitle').textContent = t.campus ? `${t.campus.name} TAG` : 'TAG';
  $('tagSubtitle').textContent = [t.entry_term && `${t.entry_term.label} entry`, tagPlanNames(entry), t.major]
    .filter(Boolean).join(' · ');
  $('tagIcon').className = `w-10 h-10 shrink-0 rounded-lg grid place-items-center ${look.tone.icon}`;
  $('tagBody').innerHTML = `
    <section class="rounded-xl border ${look.tone.box} p-4">
      <span class="${CHIP} ${look.tone.chip} bg-white/60 dark:bg-white/5"><i class="fa-solid ${look.icon}"></i> ${look.label}</span>
      <p class="mt-2 font-semibold leading-snug">${esc(t.headline)}</p>
      <p class="mt-1 text-sm ${look.tone.sub}">${esc(t.summary)}</p>
    </section>
    ${tagChecksHTML(t)}
    ${t.reference ? tagReferenceHTML(t) : ''}
    ${tagReviewHTML(t.review_items)}`;
  $('tagFooter').innerHTML = tagSourceHTML(t);
}

/** Campus requirements in full; shared/eligibility items the planner can't check yet folded into one list. */
function tagChecksHTML(t) {
  const later = t.checks.filter(c => c.status === 'unknown' && (c.group === 'shared' || c.group === 'eligibility'));
  const sections = TAG_CHECK_GROUPS.map(([group, heading]) => {
    const rows = t.checks.filter(c => c.group === group && !later.includes(c));
    if (!rows.length) return '';
    const title = heading === null ? `${t.campus ? t.campus.name : 'Campus'} requirements` : heading;
    return `
    <section>
      <h3 class="mb-2 text-xs font-semibold uppercase tracking-wider text-slate-500 dark:text-slate-400">${esc(title)}</h3>
      <ul class="space-y-3">${rows.map(tagCheckHTML).join('')}</ul>
    </section>`;
  }).join('');
  return sections + (!later.length ? '' : `
    <details class="group/later rounded-lg ring-1 ring-slate-200 dark:ring-slate-700/70 px-3 py-2.5">
      <summary class="cursor-pointer list-none flex items-center justify-between gap-2 text-sm font-medium">
        <span><i class="fa-solid fa-circle-question mr-1.5 text-slate-400 dark:text-slate-500"></i>${later.length} more can't be checked from the planner yet</span>
        <i class="fa-solid fa-chevron-down text-xs text-slate-400 transition-transform group-open/later:rotate-180"></i>
      </summary>
      <p class="mt-1 text-xs text-slate-500 dark:text-slate-400">Units, UC-E/UC-M courses, grades, application steps and disqualifying conditions aren't collected by the planner yet.</p>
      <ul class="mt-3 space-y-3">${later.map(tagCheckHTML).join('')}</ul>
    </details>`);
}

function tagCheckHTML(c) {
  const s = TAG_CHECK[c.status] || TAG_CHECK.unknown;
  const facts = [['Required', c.required], ['You', c.student], ['Deadline', c.deadline]].filter(([, v]) => v);
  const links = c.urls.map(safeUrl).filter(Boolean);
  return `
        <li class="flex gap-3">
          <i class="fa-solid ${s.icon} mt-0.5 ${s.iconCls}" aria-hidden="true"></i>
          <div class="min-w-0 flex-1">
            <div class="flex items-baseline justify-between gap-2">
              <span class="text-sm font-medium">${esc(c.title)}</span>
              ${s.label ? `<span class="shrink-0 text-[11px] font-semibold ${s.labelCls}">${s.label}</span>` : ''}
            </div>
            ${facts.length ? `<dl class="mt-1 grid grid-cols-[auto_1fr] gap-x-2 gap-y-0.5 text-xs">${facts.map(([k, v]) => `
              <dt class="text-slate-500 dark:text-slate-400">${k}</dt><dd class="text-slate-700 dark:text-slate-200 break-words">${esc(v)}</dd>`).join('')}</dl>` : ''}
            <p class="mt-1 text-xs leading-relaxed text-slate-600 dark:text-slate-300">${esc(c.explanation)}</p>
            ${links.length ? `<div class="mt-1 flex flex-wrap gap-x-3 gap-y-1">${links.map(u => `
              <a href="${esc(u)}" target="_blank" rel="noopener noreferrer"
                class="inline-flex items-center gap-1 text-xs font-medium text-blue-700 dark:text-blue-300 underline underline-offset-2">
                ${esc(new URL(u).hostname.replace(/^www\./, ''))} <i class="fa-solid fa-arrow-up-right-from-square text-[10px]"></i></a>`).join('')}</div>` : ''}
          </div>
        </li>`;
}

/** No rules for the plan's term: the campus's latest published rules, for context only (never the status). */
function tagReferenceHTML(t) {
  const r = t.reference;
  const look = tagLook(r.status);
  return `
    <details class="group rounded-xl ring-1 ring-slate-200 dark:ring-slate-700/70 p-4" open>
      <summary class="cursor-pointer list-none">
        <span class="flex items-center justify-between gap-2 text-xs font-semibold uppercase tracking-wider text-slate-500 dark:text-slate-400">
          For reference · ${esc(r.rules_term.label)} rules
          <i class="fa-solid fa-chevron-down transition-transform group-open:rotate-180"></i>
        </span>
        <span class="mt-1 block text-sm text-slate-600 dark:text-slate-300">
          ${esc(t.campus.name)}'s most recent published TAG rules. They don't decide your ${esc(t.entry_term.label)} status.</span>
        <span class="mt-2 flex flex-wrap items-center gap-2 text-sm font-medium">
          <span class="${CHIP} ${look.tone.chip}"><i class="fa-solid ${look.icon}"></i> ${look.label}</span> ${esc(r.headline)}</span>
      </summary>
      <div class="mt-5 space-y-6">${tagChecksHTML(r)}${tagReviewHTML(r.review_items)}</div>
    </details>`;
}

function tagReviewHTML(items) {
  if (!items || !items.length) return '';
  return `
    <section>
      <h3 class="mb-2 text-xs font-semibold uppercase tracking-wider text-slate-500 dark:text-slate-400">Flagged in the TAG data</h3>
      <ul class="space-y-2">${items.map(i => `
        <li class="rounded-lg bg-slate-50 dark:bg-slate-800/50 ring-1 ring-slate-200 dark:ring-slate-800 px-3 py-2 text-xs leading-relaxed text-slate-600 dark:text-slate-300">
          ${esc(i.issue)}</li>`).join('')}
      </ul>
    </section>`;
}

function tagSourceHTML(t) {
  const d = t.source && t.source.dataset;
  const link = safeUrl(t.source && t.source.campus_tag_url);
  const asOf = d && d.accurate_as_of
    ? `, accurate as of ${new Date(`${d.accurate_as_of}T12:00:00`).toLocaleDateString('en-US', { year: 'numeric', month: 'short', day: 'numeric' })}` : '';
  return [
    d ? `Source: ${esc(d.label)} (${esc(d.publisher || 'University of California')}${asOf}${d.subject_to_change ? '; subject to change' : ''}).` : '',
    `A planning estimate, not an admission decision${t.campus ? `. Confirm with ${esc(t.campus.name)}` : ''}.`,
    link ? `<a href="${esc(link)}" target="_blank" rel="noopener noreferrer" class="font-medium underline underline-offset-2 hover:text-slate-700 dark:hover:text-slate-200">Campus TAG page <i class="fa-solid fa-arrow-up-right-from-square text-[10px]"></i></a>` : ''
  ].filter(Boolean).join(' ');
}

/* ---------------------------------------------------------------------
 * 6. PDF EXPORT (html2pdf.js) — a clean, print-styled SEP document built from the
 *    current plans (not a screenshot of the interactive cards), always in light colors.
 * ------------------------------------------------------------------- */
const PDF_OPTIONS = {
  margin: 0.5,                                                   // inches
  filename: 'My_TransferPath_SEP.pdf',
  image: { type: 'jpeg', quality: 0.98 },
  // scale 2 = crisp text; scrollX/Y 0 stop html2canvas from offsetting the capture by the page's
  // scroll position (otherwise a scrolled page yields blank page tops and cut-off content)
  html2canvas: { scale: 2, useCORS: true, scrollX: 0, scrollY: 0 },
  jsPDF: { unit: 'in', format: 'letter', orientation: 'portrait' },
  // never split a term row or a box. Only mark block-level elements .pdf-avoid, never grid cells:
  // html2pdf inserts a spacer before the element, which would take up a grid cell.
  pagebreak: { mode: ['css', 'legacy'], avoid: '.pdf-avoid' }
};
const HTML2PDF_LOCAL = 'assets/vendor/html2pdf/html2pdf.bundle.min.js';

/** html2pdf from the CDN tag in <head>, or the identical local copy if the CDN was blocked. */
function loadHtml2Pdf() {
  if (window.html2pdf) return Promise.resolve(window.html2pdf);
  return new Promise((resolve, reject) => {
    const s = document.createElement('script');
    s.src = HTML2PDF_LOCAL;
    s.onload = () => (window.html2pdf ? resolve(window.html2pdf) : reject(new Error('PDF library failed to initialize')));
    s.onerror = () => reject(new Error('Could not load the PDF library'));
    document.head.appendChild(s);
  });
}

function buildPdfDocument(resp) {
  const pw = resp.pathway;
  const today = new Date().toLocaleDateString('en-US', { year: 'numeric', month: 'long', day: 'numeric' });
  const first = resp.plans[0];
  const field = (label, value) => `
    <div><div class="text-[9px] font-semibold uppercase tracking-wider text-slate-500">${label}</div>
      <div class="text-[11.5px] text-slate-900">${value}</div></div>`;
  const ap = state.apScores.length ? state.apScores.map(a => `${esc(a.subject)} (${a.score})`).join(', ') : 'None reported';
  const tagEntries = resp.tag_evaluations || [];
  const tagHTML = tagEntries.map(({ evaluation: t }) => {
    const rules = t.reference || t;       // no rules for the plan's term: list the reference rules' checks
    const order = ['campus', 'shared', 'eligibility'];
    const rows = rules.checks.filter(c => order.includes(c.group) && c.status !== 'info' && c.status !== 'unknown')
      .sort((a, b) => order.indexOf(a.group) - order.indexOf(b.group));
    const unchecked = rules.checks.filter(c => c.status === 'unknown').length;
    const box = { eligible: 'border-emerald-300 bg-emerald-50', ineligible: 'border-amber-300 bg-amber-50' }[t.status]
      || 'border-blue-300 bg-blue-50';
    return `
    <section class="pdf-avoid mt-4 rounded-md border px-3 py-2 ${box}">
      <div class="text-[11.5px] font-semibold">Transfer Admission Guarantee (TAG) — ${esc(t.campus ? t.campus.name : state.lastRequest?.tag_university || '')}${
        t.entry_term ? `, ${esc(t.entry_term.label)} entry` : ''}: ${esc(tagLook(t.status).label)}</div>
      <div class="mt-0.5 text-[10.5px] text-slate-700">${esc(t.headline)}. ${esc(t.summary)}</div>
      ${rows.length ? `<div class="mt-1 text-[10px] text-slate-600">${t.reference ? `${esc(t.reference.rules_term.label)} rules, for reference: ` : ''}${
        rows.map(c => `${esc(c.title)}: ${esc(TAG_CHECK[c.status].label)}`).join(' · ')}${
        unchecked ? ` · ${unchecked} more not checked by the planner` : ''}</div>` : ''}
      <div class="mt-1 text-[9.5px] text-slate-500">A planning estimate from the ${
        t.source && t.source.dataset ? esc(t.source.dataset.label) : 'UC TAG matrix'}, not an admission decision.</div>
    </section>`;
  }).join('');

  const planHTML = (p, index) => {
    const s = p.summary;
    const codeOf = id => p.semesters.flatMap(sem => sem.courses).find(c => c.id === id)?.code ?? id;
    const stat = (label, value) => `
      <div class="rounded-md border border-slate-300 px-2.5 py-1.5">
        <div class="text-[9px] font-semibold uppercase tracking-wider text-slate-500">${label}</div>
        <div class="text-[13px] font-bold text-slate-900">${esc(value)}</div></div>`;
    const rows = p.semesters.map(sem => {
      const summer = sem.season === 'Summer';
      const courses = sem.is_padding
        ? `<div class="text-emerald-700">Transfer-ready — no required courses. Ending on this Spring lines up Fall ${sem.year} admission.</div>`
        : sem.courses.length ? sem.courses.map(c => `
            <div class="flex justify-between gap-3">
              <span>${c.type === 'ge_slot'
                ? `<span class="font-semibold">GE</span> — ${esc(c.title)} <span class="text-slate-500">(any course on the list)</span>`
                : `<span class="font-semibold">${esc(c.code)}</span> — ${esc(c.title)}${c.type === 'prerequisite'
                  ? ' <span class="text-slate-500">(prerequisite)</span>' : c.is_ge && c.satisfies.length
                  ? ` <span class="text-slate-500">(GE: ${esc(c.satisfies[0])})</span>` : ''}`}</span>
              <span class="shrink-0 text-slate-500">${c.units} u</span>
            </div>`).join('')
        : '<div class="text-slate-500">No courses this term.</div>';
      return `
        <div class="pdf-avoid grid grid-cols-[112px_1fr_56px] gap-3 px-3 py-2 border-t border-slate-200 ${summer ? 'bg-amber-50' : ''}">
          <div class="font-semibold text-slate-900">${esc(sem.term)}${summer ? `<div class="text-[9px] font-normal text-amber-700">Summer · max ${sem.max_units} u</div>` : ''}</div>
          <div class="space-y-0.5">${courses}</div>
          <div class="text-right font-semibold">${sem.units} / ${sem.max_units}</div>
        </div>`;
    }).join('');
    const reqs = p.requirements.map(r => `
      <div class="pdf-avoid"><span class="font-semibold text-slate-800">${esc(r.name)}:</span>
        ${r.courses.map(c => `${esc(c.code)} <span class="text-slate-500">(${c.status === 'completed' ? 'completed' : esc(c.term || 'planned')})</span>`).join(', ')}</div>`).join('');
    // Requirements are a plain single-column list (block flow, not a grid) so .pdf-avoid spacers can't shift cells

    return `
      ${index ? '<div class="html2pdf__page-break"></div>' : ''}
      <section class="mt-5">
        <div class="pdf-avoid">
          <div class="flex items-baseline justify-between gap-4 border-b-2 border-slate-800 pb-1">
            <h2 class="text-[16px] font-bold text-slate-900">${esc(p.name)} · ${esc(p.label)}</h2>
            <div class="text-[10px] text-slate-600">Up to ${p.max_units_regular} units per Fall/Spring term · ${p.max_units_summer} in Summer</div>
          </div>
          <p class="mt-1 text-[10.5px] text-slate-600">${esc(p.description)}</p>
          <div class="mt-2 grid grid-cols-4 gap-2">
            ${stat('Terms', s.terms_needed)}${stat('Total units', s.total_units)}
            ${stat('Transfer target', s.transfer_admission_term ? s.transfer_admission_term + ' admission' : '—')}
            ${stat('Units per term', (s.min_units_regular_term === s.max_units_regular_term
              ? `${s.min_units_regular_term}` : `${s.min_units_regular_term}–${s.max_units_regular_term}`)
              + (s.target_units_per_term ? ` (≈${s.target_units_per_term})` : ''))}
          </div>
          ${p.critical_path.length ? `<p class="mt-2 text-[10.5px]"><span class="font-semibold">Critical path:</span>
            ${p.critical_path.map(id => esc(codeOf(id))).join(' → ')}
            <span class="text-slate-500">(longest prerequisite chain — a delay here delays transfer)</span></p>` : ''}
          ${s.major_prep_in_summer.length ? `<p class="mt-1 text-[10.5px] text-amber-700">Major prep scheduled in summer: ${s.major_prep_in_summer.map(esc).join(', ')}</p>` : ''}
        </div>
        <div class="mt-2 rounded-md border border-slate-300 overflow-hidden">
          <div class="grid grid-cols-[112px_1fr_56px] gap-3 px-3 py-1.5 bg-slate-100 text-[9.5px] font-semibold uppercase tracking-wider text-slate-600">
            <div>Term</div><div>Courses</div><div class="text-right">Units</div>
          </div>
          ${rows}
          <div class="pdf-avoid grid grid-cols-[112px_1fr_56px] gap-3 px-3 py-1.5 border-t border-slate-300 bg-slate-50 font-semibold">
            <div>Total</div><div></div><div class="text-right">${s.total_units}</div>
          </div>
        </div>
        <h3 class="pdf-avoid mt-3 text-[12px] font-semibold text-slate-900">Requirements covered</h3>
        <div class="mt-1 space-y-1 text-[10px] text-slate-700">${reqs}</div>
      </section>`;
  };

  const doc = document.createElement('div');
  doc.className = 'bg-white text-slate-900 font-sans text-[11px] leading-snug';
  doc.innerHTML = `
    <header class="pdf-avoid border-b-2 border-blue-600 pb-3">
      <div class="flex items-start justify-between gap-4">
        <div>
          <div class="text-[9.5px] font-semibold uppercase tracking-widest text-blue-700">TransferPath · Student Educational Plan</div>
          <h1 class="mt-1 text-[22px] font-bold tracking-tight text-slate-900">Official TransferPath SEP</h1>
          <div class="mt-0.5 text-[12px] text-slate-600">${esc(pw.degree)} · ${esc(pw.university)}</div>
        </div>
        <div class="text-right text-[10.5px] text-slate-600">
          <div><span class="font-semibold text-slate-800">Prepared:</span> ${esc(today)}</div>
          ${first && first.summary.transfer_admission_term ? `<div><span class="font-semibold text-slate-800">Earliest transfer:</span> ${esc(first.summary.transfer_admission_term)}</div>` : ''}
        </div>
      </div>
    </header>
    <section class="pdf-avoid mt-3 grid grid-cols-3 gap-x-6 gap-y-2.5">
      ${field('Community college', esc(pw.college))}
      ${field('Target university', esc(pw.university))}
      ${field('Intended major', `${esc(pw.major)}<span class="text-slate-500"> (${esc(pw.degree)})</span>`)}
      ${field('GE pathway', esc(pw.ge_pathway))}
      ${field('Starting term', esc(pw.start_term))}
      ${field('Summer classes', pw.include_summer ? 'Included (up to 9 units)' : 'Not included')}
      ${field('Completed courses', pw.completed_courses.length ? pw.completed_courses.map(esc).join(', ') : 'None')}
      ${field('AP scores', ap)}
      ${field('TAG campus', tagEntries.length
        ? esc(tagEntries[0].evaluation.campus ? tagEntries[0].evaluation.campus.name : state.lastRequest?.tag_university || '')
        : 'None selected')}
    </section>
    ${resp.warnings.length ? `<section class="pdf-avoid mt-3 text-[10px] text-slate-600">
      <div class="font-semibold text-slate-800">Plan notes</div>
      <ul class="mt-0.5 space-y-0.5">${resp.warnings.map(w => `<li>• ${esc(w)}</li>`).join('')}</ul></section>` : ''}
    ${tagHTML}
    ${resp.plans.map(planHTML).join('')}
    <footer class="pdf-avoid mt-6 border-t border-slate-300 pt-3 text-[9.5px] text-slate-500">
      <p>Generated by TransferPath on ${esc(today)} from uploaded articulation data. Course offerings, TAG rules and transfer
        requirements change each year — review this plan with your academic counselor and confirm courses on ASSIST.org before registering.</p>
      <div class="mt-6 grid grid-cols-2 gap-10 text-[10px] text-slate-700">
        <div><div class="h-6 border-b border-slate-400"></div><div class="mt-1">Student signature / date</div></div>
        <div><div class="h-6 border-b border-slate-400"></div><div class="mt-1">Counselor signature / date</div></div>
      </div>
    </footer>`;
  return doc;
}

async function exportPlanToPDF() {
  if (!state.response || state.loading) return;
  const btn = $('exportPdfBtn');
  const status = $('exportPdfStatus');
  const label = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<i class="fa-solid fa-circle-notch fa-spin"></i> Preparing PDF…';
  status.textContent = '';
  try {
    const html2pdf = await loadHtml2Pdf();
    // html2canvas re-lays out the page in a scrollbar-less copy; give it the real layout width so the
    // centered export container doesn't shift by half a scrollbar and get clipped on the right.
    const options = { ...PDF_OPTIONS,
      html2canvas: { ...PDF_OPTIONS.html2canvas, windowWidth: document.documentElement.clientWidth } };
    await html2pdf().set(options).from(buildPdfDocument(state.response)).save();
    status.textContent = `Saved ${PDF_OPTIONS.filename}`;
  } catch (e) {
    console.error('PDF export failed:', e);
    status.textContent = `Couldn't create the PDF: ${e.message || e}`;
  } finally {
    btn.disabled = false;
    btn.innerHTML = label;
  }
}

$('exportPdfBtn').addEventListener('click', exportPlanToPDF);

/* ---------------------------------------------------------------------
 * 7. ROUTING + STARTUP — two hash routes on this one page:
 *      #/planner         the form (also the default, with no hash)
 *      #/planner/result  the generated plan as a full-width workspace
 *    <html data-view> selects the view; index.html styles each view off it (group-data-[view=…]/app).
 *    Generate pushes a history entry, so browser Back returns to the form. The form's values live in the
 *    page (inputs + `state`), so switching views never resets them. The last plan and the request that
 *    built it are kept in sessionStorage, so a refresh restores both; the result route without a plan
 *    redirects to the form.
 * ------------------------------------------------------------------- */
const ROUTES = { planner: '#/planner', result: '#/planner/result' };
const SAVED_PLAN_KEY = 'transferpath.plan';   // also read by the <head> script in index.html

const currentView = () => document.documentElement.dataset.view;

function savePlan(request, response) {
  try {
    if (request) sessionStorage.setItem(SAVED_PLAN_KEY, JSON.stringify({ request, response }));
    else sessionStorage.removeItem(SAVED_PLAN_KEY);
  } catch (e) {}   // storage blocked or full: the plan still works until the page is reloaded
}

function loadSavedPlan() {
  try {
    const saved = JSON.parse(sessionStorage.getItem(SAVED_PLAN_KEY));
    return saved && saved.request && saved.response && Array.isArray(saved.response.plans) ? saved : null;
  } catch (e) {
    return null;
  }
}

/** Show the view for the current URL. */
function showRoute() {
  let view = location.hash === ROUTES.result && state.response ? 'result' : 'planner';
  // The result route without a plan, no hash, or an unknown hash all mean the form: name it in the URL
  // (replace, so Back never lands on an empty result page)
  if (location.hash !== ROUTES[view]) history.replaceState(null, '', ROUTES[view]);
  const from = currentView();
  document.documentElement.dataset.view = view;
  document.title = view === 'result' ? 'Your plan · TransferPath SEP Planner' : 'TransferPath SEP Planner';
  if (view === 'result') {
    if (from !== 'result') $('resultView').classList.remove('no-reveal');
    window.scrollTo(0, 0);
  } else {
    closeTermFocus();
    closeGePanel();
    closeTagPanel();
    if (from === 'result') window.scrollTo(0, state.plannerScroll);   // back where the form was left
  }
}

/**
 * Form -> result page. Where the View Transitions API exists, the form zooms toward the viewer from the centre
 * of the screen and the result page zooms in after it (styles in index.html); otherwise, and with reduced
 * motion, the page switches directly.
 */
let pageTransition = null;

function openResultPage() {
  if (pageTransition) return;                                  // a second click while zooming
  state.plannerScroll = window.scrollY;
  const root = document.documentElement;
  const centreOf = el => {
    const r = el.getBoundingClientRect();
    return `${innerWidth / 2 - r.left}px ${innerHeight / 2 - r.top}px`;
  };
  const go = () => {
    history.pushState({ fromPlanner: true }, '', ROUTES.result);
    showRoute();
    root.style.setProperty('--zoom-to', centreOf($('resultView')));
  };
  const zoom = document.startViewTransition && !window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  $('resultView').classList.toggle('zoom-enter', !!zoom);     // the result entrance waits for the zoom
  if (!zoom) return go();
  root.style.setProperty('--zoom-from', centreOf($('plannerView')));
  root.classList.add('page-zoom');                             // names the zooming parts (index.html)
  pageTransition = document.startViewTransition(go);
  pageTransition.finished.finally(() => {
    root.classList.remove('page-zoom');
    pageTransition = null;
  });
}

// ← Edit Plan: return to the form's own history entry when there is one, so browser history stays linear
$('editPlanBtn').addEventListener('click', e => {
  e.preventDefault();
  if (history.state && history.state.fromPlanner) history.back();
  else { history.replaceState(null, '', ROUTES.planner); showRoute(); }
});

window.addEventListener('hashchange', showRoute);             // browser Back/Forward, edited URLs
if ('scrollRestoration' in history) history.scrollRestoration = 'manual';   // showRoute() handles scrolling

const savedPlan = loadSavedPlan();
if (savedPlan) applyRequestToForm(savedPlan.request);
renderTags();
renderApTags();
renderTagOptions();
renderStartTerms();
renderDevTools();
applyDefaultOptions();
init();
if (savedPlan) renderPlans(savedPlan.response, savedPlan.request);
showRoute();
