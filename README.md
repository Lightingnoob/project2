# TransferPath SEP Planner

TransferPath SEP Planner helps community college students build a Student Educational Plan (SEP) that maps out the courses they need to transfer to their target four-year university.

## Run it

```
pip install -r requirements.txt
uvicorn main:app --reload        # app at http://127.0.0.1:8000/, API docs at /docs
python -m unittest discover -s tests -v
```

The UI loads its CSS, fonts and icons from `assets/`, not from CDNs. It is fully styled offline, on networks that block CDNs, and in editor preview panes. `assets/tailwind.css` is prebuilt from the classes in `index.html` and `ui.js`. If you add new Tailwind classes, rebuild it (the Tailwind CDN script in `<head>` covers new classes in the meantime when online):

```
npx tailwindcss@3.4.17 -c tailwind.config.js -i tailwind.input.css -o assets/tailwind.css --minify
```

The visual theme is plain CSS in `assets/theme.css`, so changing it needs no rebuild. It holds the slowly drifting rose-to-mint backdrop (static under `prefers-reduced-motion`), the frosted-glass surface classes (`surface`, `surface-feature`, `surface-bar`, `surface-panel`, `surface-tile`), the planner form's control classes (`field`, `switch`, `menu`, `btn-primary`, `btn-tonal`) and the color tokens for light and dark mode.

Everything the planner uses is loaded from `data/` once at startup (see [Data](#data-data)); `GET /data-status` shows what loaded. A pathway without an articulation file shows "N/A - Transfer data for this specific pathway has not been uploaded yet." No mock data is needed for real pathways. **Developer: Inject Mock Data** (or `POST /dev/mock-data`) still adds one illustrative demo pathway, Chabot College -> UC Berkeley -> Computer Science, marked `source: "mock"`; clearing it leaves loaded data alone.

## Data (`data/`)

Each folder has one responsibility and its own store (`datastores/`, plus `articulation/` for agreements). Files are recognized by their content, not their names, and every dataset keeps its academic year.

| Folder | Holds | Store | Used for |
|---|---|---|---|
| `articulation/` | ASSIST major agreements (requirement trees) | `articulation.PathwayStore` | major preparation |
| `catalogs/` | college course catalogs | `CatalogStore` | course search, canonical codes, titles, units, catalog text |
| `prerequisites/` | college prerequisite registry (AND / OR / CONDITION, timings) | `PrerequisiteStore` | scheduling order |
| `ge/` | Cal-GETC lists per college and year; the UC 7-course pattern | `GEStore` | GE requirements and GE course options |
| `ap/` | the college's AP chart; campus AP charts | `APStore` | AP course waivers, GE areas, campus credit (reported) |
| `reference/` | raw catalog pages, fill-in templates | none | audit / debugging only, never planning |

`uc_tag_requirements_*.json` and `uc_major_affiliations.json` belong to the TAG engine.

## Planner pipeline (`planner.py`)

`POST /generate-sep` for a loaded pathway runs: canonical institutions, major and academic year -> the agreement -> completed courses (catalog codes, former codes like `ENGL 1` -> `ENGL C1000`, cross-listings like `CSCI 28` -> `MTH 8`) -> the college's AP waivers -> remaining major prep and its prerequisites -> remaining GE -> major-prep/GE overlap -> `sep_engine` -> a structured plan.

- **Articulation (`articulation/assist.py`).** Requirement trees (`COURSE`, `AND`, `OR`, nested groups, `MINIMUM`) are expanded into valid *alternatives*. An OR of single courses stays one choice for the engine's Dijkstra step; ORs of bundles or groups and MINIMUM selections become separate alternatives, and the planner schedules the one with the fewest units (the response names the branch taken and how many exist). An OR is never turned into an AND. `no_course_articulated` is returned as `remaining_after_transfer`, never scheduled.
- **Prerequisites (`datastores/prerequisites.py`).** The registry is the source for ordering; an agreement's embedded records are a fallback, and the catalog's flat `prerequisite_course_ids` are display text only. `before` -> earlier term; `before_or_concurrent` and `same_term` -> same term or earlier (same-term is reported). An OR takes an alternative already satisfied or already in the plan; if any alternative is a CONDITION (placement, approval) no course is added and the course is flagged for review; otherwise the cheapest researched course-only alternative is added. Unresolved rules, unresearched courses and courses without catalog units become review items, never guesses.
- **GE (`datastores/ge.py`).** Uses the college's own list for the start term's academic year (else the latest earlier one, noted), never another college's. Completed courses and AP exams fill slots first (each course counts toward one area; a 5C lab may also count with its 5A/5B area, as the list states; Area 4 needs two disciplines). Remaining slots enter the engine as placeholder courses that compete with major-prep courses on the same list, so a major course on the list (MTH 1 for Area 2, PHYS 4A for 5A + lab) fills the slot instead of adding a duplicate. The UC 7-course pattern is a UC admission requirement: it applies to UC targets only and has no college course list in the data.
- **AP (`datastores/ap.py`).** Three separate effects: `community_college_effect` (the college chart's course waivers; choose-one waivers credit one course), `ge_effect` (the chart's Cal-GETC areas) and `target_campus_effect` (the campus chart, reported with its review status and never applied: university credit does not complete a college course).
- **Output.** Every scheduled course has a `type`: `major_prep`, `prerequisite`, `ge_slot` (with `ge.requirement_id`, e.g. `3A`) or `ge_course`. Each plan has a `ge` block with every requirement's status. The response adds `remaining_after_transfer`, `data_sources` (files and academic years), `ap_evaluations`, `review_items`, `admission_requirements` (UC only) and structured `notes`; the UI shows notes in one collapsed "Plan notes" list.

## Scheduling engine (`sep_engine/`)

Python 3.10+ package that turns articulation data into SEP plans. `python -m sep_engine [--summer]` prints plans for the mock data.

`generate_sep()` first works out the workload, then builds the plans:

- **Total required units** = the major prep + GE still to take (the minimum-units course selection).
- **Average over 2 years** = total ÷ 4 Fall/Spring semesters (a summer counts as half a semester when enabled).
  - **≤ 15 units per semester (normal):** one balanced plan, over the shortest span that keeps the average at 15 or less.
  - **> 15 (heavy):** Plan A **Fast Track** (2 years, ~16–18 units) and Plan B **Balanced Track** (3 years). If 2 years isn't possible even at 18 units, only one plan is returned, and the response says why.

Each plan runs through:

1. **Dijkstra** (`pathway.py`) picks courses for each multiple-choice requirement group with the lowest total units.
2. **Topological sort** (`toposort.py`, Kahn's algorithm) orders the selected courses by prerequisites and reports any prerequisite cycle.
3. **CSP backtracking** (`scheduler.py`) assigns courses to real terms ("Fall 2026", "Summer 2027", ...) starting from the student's start term. It uses DFS with MRV and forward checking, then **load balancing**:
   - `Target_Units_Per_Term` = total ÷ semesters. The scheduler re-solves with a soft cap of target + 2 units per term, placing major prep first along its prerequisite chain and then GE into the lightest terms. A final pass moves single GE courses (major prep only if no GE move helps) until terms are as even as the constraints allow, e.g. 14/13/13/13 rather than 18/18/17 and a nearly empty last term.
   - Hard caps: 18 units per Fall/Spring term, 9 in Summer (only when enabled).
   - Plans always **end in a Spring** term, for Fall transfer admission.
   - GE courses get the Summer share first. Major prep stays out of Summer unless no plan of the same length exists without it.
   - Fall-/Spring-only offerings, co-requisites and meeting-time conflicts are respected.

Real pathways are prepared by `planner.py` (above). The engine itself takes `SEPRequest` in `sep_engine/models.py` (`catalog`, `agreement`, `profile`). Prerequisite closures stop at completed courses: a completed course's own prerequisites are never added back.

## Articulation data (`articulation/`)

Each agreement is one JSON file in `data/articulation/`: which community college courses a university accepts for a major. Two schemas load: ASSIST-extracted agreements (schema 1.x with `requirement_groups` operators and `source_requirement` expressions; `articulation/assist.py`) and the older flat `transfer_articulation` schema (`articulation/schema.py`). A new pathway needs a file, not code.

- **Institutions:** each file declares canonical ids with official names (`chabot`, `csueb`). Everyday spellings resolve to the same id, e.g. "Cal State East Bay", "California State University, East Bay" and "CSUEB", or "UC Berkeley" and "UCB". Lookup uses the ids, never raw strings.
- **Lookup:** college and university ids, the major with the degree kept separate ("Computer Science, B.S." = "Computer Science" + "B.S."), and the start term's academic year (Fall 2026 and Spring 2027 are both 2026-2027). It uses that year's agreement, otherwise the latest earlier one with a warning, and never a later one. Loaded data wins over mock data.
- **Unsupported structures** (an unknown operator, a course the file doesn't describe, missing units, a tree with too many combinations) make a pathway **unsupported**: `/generate-sep` returns 422 with the reason. Nothing is flattened or guessed.
- **Prerequisites** come from `data/prerequisites/` (see the pipeline). Legacy files fall back to their own `prereqs`/`coreqs`.
- **Diagnostics:** `GET /pathways` lists the available pathways, the unsupported agreements and the skipped files, each with its reason. `POST /pathways/reload` re-reads the folder without a restart. Every plan's `articulation` block names the file, year and source URL it came from.

## API (`main.py`)

`POST /generate-sep` takes `college`, `university`, `major`, `ge_pathway`, `completed_courses`, `ap_scores`, `start_term` (e.g. `"Fall 2026"`) and `include_summer`. It returns 404 when no articulation data covers the pathway, and 422 (with the reason) when the data exists but can't be planned, or when the UC 7-course pattern is chosen for a non-UC target. Otherwise it returns `articulation` (where the major prep comes from, per-requirement status), `notes`/`warnings` and `plans` (plus the fields listed under the pipeline), and each plan has:

- `semesters`: real term names, unit caps and padding flags
- `graph`: Cytoscape `{nodes, edges}` (not used by the UI, which shows semester cards only)
- `critical_path` / `critical_path_edges`
- `requirements` and `summary`

Pydantic response models validate the whole payload, including consecutive real terms and the Spring ending.

Other endpoints: `GET /data-status` (what loaded, with years and skipped files), `GET /institutions` (colleges and universities named by the data), `GET /majors?college=&university=` (majors with articulation data), `GET /courses/search?institution=chabot&q=calc` (compact `{code, title, units}`), `GET /courses/resolve`, `GET /courses/detail`, `GET /ge/options?institution=&requirement_id=&start_term=` (the courses that fill one GE slot), `GET /ap/exams`, `POST /ap/evaluate`, `GET /pathways`, `POST /pathways/reload`.

Optional `tag_university` (a campus id from `GET /tag/campuses`, or its name) adds `tag_evaluations`: one TAG evaluation per distinct transfer term among the plans. It's `null` when no TAG campus is chosen, because TAG is optional.

## TAG eligibility (`tag_engine/`)

TAG is a separate layer. It reads the planner's request and plans but never changes them, and admission criteria never become course prerequisites.

- **Data:** `data/uc_tag_requirements_2027_2028.json`, the UC 2027–28 TAG matrix, is the source of truth. It's kept verbatim, loaded read-only and validated, and `null` stays "unresolved", never "no requirement". To support a new admission cycle, add another `data/uc_tag_requirements_*.json` file. The registry picks the dataset whose `coverage.entry_terms` include the student's entry term, so no code changes are needed.
- **Major affiliations:** `data/uc_major_affiliations.json` maps campus → school/college → major → degree for the TAG campuses that set rules by unit: UC Davis, UC Irvine, UC Merced, UC Riverside and UC Santa Barbara. It keeps official organization names and types, with aliases where the TAG matrix spells a unit differently. Every mapping cites its source: the 2026–27 catalogs for UC Irvine, UC Davis and UC Merced, and the official admissions and college pages for UC Santa Barbara and UC Riverside. The TAG engine uses it for `all_majors_in_school` and `all_majors_in_college` exclusions, such as every major in UC Irvine's Donald Bren School of Information and Computer Sciences, and for GPA minimums set by college or school. A school or college the student names wins. A major the file doesn't list, lists as `unresolved` (e.g. majors run jointly by two schools), or offers under several units (UC Santa Barbara Mathematics) is never placed in one. A unit rule that applies to only some of the possibilities comes back `needs_review`, never as a pass. To cover more majors or campuses, edit the file; no code changes are needed. UC Santa Cruz isn't included, because no TAG rule refers to its units.
- **Code:** `models.py` has the types, `data.py` loads, normalizes and registers datasets, `affiliations.py` loads the affiliation data and resolves majors (`MajorAffiliationRegistry.lookup(university, major, degree)`), `selectors.py` holds the scope-aware exclusion matcher, the GPA resolver and the GE/major-prep lookups, and `evaluator.py` runs `evaluate_tag_eligibility(dataset, campus, entry_term, student)`. Each evaluation reports the `major_affiliation` it used, with its sources.
- **Statuses:** results are `eligible`, `ineligible`, `needs_review` or `not_applicable`, and each `TagCheck` is `met`, `not_met`, `unknown`, `manual_review` or `info`. Shared and campus requirements must both pass, for the entry term only. Specifically:
  - A major GPA exception overrides its college/school rule. When the major's college/school is unknown, the GPA is ambiguous and needs review; it's never taken as the lowest value.
  - Announced future exclusions, such as UC Irvine's Fall 2028 list, apply from their term only.
  - When no dataset covers the entry term, the latest published rules are attached as `reference`, and they never set the status.
- **Deliberately `needs_review`:** the planner doesn't collect GPA, units, UC-E/UC-M courses or the disqualifying conditions yet. The matrix flags the seven-course deadline for verification and doesn't list major-prep courses. This dataset says it isn't sufficient for a final automated decision, so it can't produce `eligible`.
- **Endpoints:** `GET /tag/campuses` lists the TAG University options from the dataset. `POST /tag/evaluate` takes `{campus, entry_term, student}`, where `student` is a `TagStudentProfile`, and returns a `TagEvaluation` independent of SEP generation.
- **Form status:** once both an intended major and a TAG University are chosen, the form calls `POST /tag/evaluate` for the campus's Fall entry term and shows the major-exclusion result under the TAG University field: *TAG available for this major*, *TAG unavailable for this major* or *TAG eligibility needs review*. No plan needs to be generated first.
