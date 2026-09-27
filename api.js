/* =====================================================================
 * api.js — HTTP client for the TransferPath FastAPI backend (main.py)
 *
 * The UI talks to the backend ONLY through these hooks:
 *
 *   fetchInstitutions()        -> { colleges: [{ id, name, has_articulation }], universities: [{ id, name, system,
 *                                   has_articulation }] }          (from the loaded datasets, not hardcoded)
 *   fetchMajors(cc, uni)       -> { majors: [{ major, degree, label, academic_year, concentration }] }
 *                                   majors with articulation data for this college + university
 *   searchCourses(cc, q)       -> [{ code, title, units }] | null            (null = no catalog for the college)
 *   resolveCourse(cc, code)    -> { code, title, units, matched_by } | null  (canonical code; null = not in catalog)
 *   fetchApExams()             -> { exams: [{ id, name }] }
 *   evaluateAp(request)        -> { evaluations: [{ exam_id, community_college_effect, ge_effect, target_campus_effect }] }
 *        request = { college, university, ap_scores: [{ subject, score }] }
 *   generateSep(request)       -> SEPResponse | null                        (404 -> null = "not uploaded yet")
 *        request = { college, university, major, ge_pathway, completed_courses,
 *                    ap_scores: [{ subject: 'AP Calculus BC', score: 5 }],
 *                    tag_university: 'riverside' | null,       (id from fetchTagCampuses; a name also works)
 *                    start_term: 'Fall 2026', include_summer: false }
 *        response.tag_evaluations = [{ plan_ids: ['A'], evaluation: TagEvaluation }] | null
 *                    one per distinct transfer term; TagEvaluation = { status: 'eligible' | 'ineligible' |
 *                    'needs_review' | 'not_applicable', headline, summary, checks: [...], reference, ... }
 *   fetchTagCampuses()         -> { campuses: [{ id, name, entry_terms: [{ key, label }] }], datasets }
 *   evaluateTag(request)       -> TagEvaluation, without generating a plan (the form's TAG major check)
 *        request = { campus: 'irvine', entry_term: 'fall_2027', student: { intended_major: 'Computer Science' } }
 *   fetchMockDataStatus() / injectMockData() / clearMockData()  (dev-only injector)
 *   fetchGeOptions(slot)       -> GE options payload { status, name, rule, options: [{ code, title, units,
 *                                   prerequisite, also_counts_for }] } | null   (null = slot has no real GE data)
 *        slot = one GE slot of a semester (ui.js geSlotsOf); its `ge` block comes from the plan
 *
 * Errors: network failure -> ApiError { offline: true }; other HTTP errors ->
 * ApiError with the server's `detail` message.
 *
 * The backend loads every dataset under data/ at startup (GET /data-status shows what loaded). The
 * Developer: Inject Mock Data button only adds a demo pathway; real pathways don't need it.
 * ===================================================================== */

// Always the local FastAPI server, however the page itself is served (uvicorn :8000,
// VS Code Live Server :5500, another dev server, or file://). CORS in main.py allows all origins.
// Override by setting window.SEP_API_BASE before this script loads.
const API_BASE = window.SEP_API_BASE ?? 'http://127.0.0.1:8000';

class ApiError extends Error {
  constructor(message, { status = 0, offline = false } = {}) {
    super(message);
    this.status = status;
    this.offline = offline;
  }
}

async function apiRequest(path, { method = 'GET', body, allow404 = false } = {}) {
  const url = API_BASE + path;
  console.log(`[TransferPath] ${method} ${url}`, body ?? '');   // proves the request is actually firing
  const res = await fetch(url, {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined
  }).catch(error => {
    // fetch only rejects when NO HTTP response arrives: server not running, wrong port, or CORS blocked.
    // (A backend crash is still a response — it shows up below as HTTP 500, not here.)
    console.error('Fetch Error Details:', error);
    console.error(`[TransferPath] No response from ${method} ${url}. Network/CORS failure or server not running — ` +
      `open ${API_BASE}/health (should show {"status":"ok"}) and check the uvicorn terminal.`);
    return null;
  });
  if (!res) {
    throw new ApiError(`Can't reach the TransferPath API at ${API_BASE}.`, { offline: true });
  }
  if (allow404 && res.status === 404) {
    console.warn(`[TransferPath] HTTP 404 ${res.statusText} from ${method} ${url} — expected: no data uploaded for this request.`);
    return null;
  }
  const data = await res.json().catch(() => null);
  if (!res.ok) {
    const detail = data && data.detail;
    const msg = typeof detail === 'string' ? detail
      : Array.isArray(detail) ? detail.map(d => d.msg).join('; ')
      : res.status >= 500 ? `HTTP ${res.status} ${res.statusText}: the backend hit an error — see the uvicorn terminal for the traceback.`
      : `HTTP ${res.status} ${res.statusText}`;
    console.error(`[TransferPath] HTTP ${res.status} ${res.statusText} from ${method} ${url}`,
      res.status >= 500 ? '(backend error: check the uvicorn terminal)' : '', detail ?? '');
    throw new ApiError(msg, { status: res.status });
  }
  return data;
}

const q = params => new URLSearchParams(params).toString();

const fetchInstitutions = () => apiRequest('/institutions');

const fetchMajors = (college, university) => apiRequest(`/majors?${q({ college, university })}`);

const searchCourses = async (college, text) => {
  const data = await apiRequest(`/courses/search?${q({ institution: college, q: text, limit: 12 })}`, { allow404: true });
  return data && data.results;
};

const resolveCourse = (college, code) =>
  apiRequest(`/courses/resolve?${q({ institution: college, code })}`, { allow404: true });

const fetchApExams = () => apiRequest('/ap/exams');

const evaluateAp = request => apiRequest('/ap/evaluate', { method: 'POST', body: request });

const generateSep = request =>
  apiRequest('/generate-sep', { method: 'POST', body: request, allow404: true });

/* TAG University options, from the TAG dataset(s) on the server */
const fetchTagCampuses = () => apiRequest('/tag/campuses');

/* One TAG evaluation for a campus + entry term + student, independent of SEP generation */
const evaluateTag = request => apiRequest('/tag/evaluate', { method: 'POST', body: request });

/* Dev-only: hackathon data injector (backend keeps the injected data until cleared or restarted) */
const fetchMockDataStatus = () => apiRequest('/dev/mock-data');
const injectMockData = () => apiRequest('/dev/mock-data', { method: 'POST' });
const clearMockData = () => apiRequest('/dev/mock-data', { method: 'DELETE' });

/* GE course options for one GE slot: the college's own GE list for the plan's academic year + its catalog.
 * Mock-data slots carry no `ge` block, so they return null (the panel then shows its empty state). */
async function fetchGeOptions(slot) {
  const ge = slot && slot.ge;
  if (!ge || !ge.requirement_id || !ge.institution_id) return null;
  const params = { institution: ge.institution_id, pathway: ge.pathway, requirement_id: ge.requirement_id,
                   lab: ge.lab_required ? 'true' : 'false' };
  if (slot.startTerm) params.start_term = slot.startTerm;
  return apiRequest(`/ge/options?${q(params)}`);
}
