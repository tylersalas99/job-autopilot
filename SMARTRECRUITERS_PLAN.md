# SmartRecruiters handler — shakeout map for the next session

Status: handler built + reviewed 2026-07-19, **zero live runs**. This file
is the working map for fixing/advancing it. Read alongside:
- `handlers/smartrecruiters.py` docstring — the DOM contract (what was
  actually captured live vs. assumed)
- `CLAUDE.md` → "SmartRecruiters specifics" + "Known gaps" item 2
- Tests: `tests/test_pipeline.py`, section "SmartRecruiters" (search
  `_SR_URL`) — 11 tests pin current behavior

## What is VERIFIED (live capture, Visa oneclick-ui, 2026-07-19)

- Apply URL formula: posting API `uuid` == publication UUID →
  `jobs.smartrecruiters.com/oneclick-ui/company/<Company>/publication/<uuid>?dcr_ci=<Company>`
- Public posting API (no auth): `api.smartrecruiters.com/v1/companies/<Company>/postings/<id>`
  — has `name`, `company.name`, `location.fullLocation`, `jobAd.sections`,
  `active`, `uuid`. Intake (`_enrich_smartrecruiters`) already uses it.
- Step-1 DOM: spl-* shadow components; stable ids (first-name-input,
  last-name-input, email-input, confirm-email-input, linkedin-input,
  facebook-input, twitter-input, website-input, file-input,
  hiring-manager-message-input); phone = `input[type=tel]`; city =
  spl-autocomplete with GENERATED id; labels use `for` + trailing `*` for
  required; Angular puts `ng-invalid` on the spl-* HOST.
- Two file inputs; avatar one comes first — resume upload is scoped to
  `spl-dropzone input[type=file]` / `#file-input`. Keep it scoped.
- Partner buttons "Apply With Indeed"/"Apply with SEEK" exist → submit
  matching must stay exact-phrase (`_submitish`).
- DataDome loads (api-js.datadome.co); challenge = captcha-delivery.com
  iframe (already in this handler's `detect_captcha`).
- OneTrust cookie banner; `#onetrust-reject-all-handler` dismisses it.

## What is ASSUMED (verify on first supervised run, in this order)

Each of these is a plausible first-run breakage. When one fails: fix in
`handlers/smartrecruiters.py`, pin with a mock-DOM test, update the
docstring contract + CLAUDE.md.

1. **Playwright `fill()` commits values through Angular** on shadow inputs
   (it dispatches input events — expected to work, never tried on THIS app).
   Symptom if wrong: fields visually filled but ng-invalid persists /
   values vanish on Next. Fix direction: dispatch extra `change`/`blur` via
   `el.evaluate`, or type() instead of fill().
2. **Phone field accepts the profile phone string as-is** into
   `input[type=tel]` (country selector untouched, assumes US default).
   Symptom: spl-phone-field stays ng-invalid. Fix: strip to national
   digits; if the country dropdown needs setting, its search input is
   `input[aria-label='Search by country/region or code']` inside
   SPL-DROPDOWN-SEARCH (captured but unused).
3. **Clicking the `spl-button` host reaches its shadow <button>** (both
   "Next" and submit). Symptom: click times out or nothing happens. Fix:
   `host.query_selector('button')` and click that; if an overlay
   intercepts, click via `el.evaluate('el => el.click()')` — note what
   worked in the docstring.
4. **City typeahead options render as `[role=option]`** (poll in
   `_poll_option`). Unverified — typing was blocked during capture.
   Symptom: types "El Paso", finds nothing, clears (field is optional on
   Visa; fine). Capture the real suggestion DOM and fix the selector.
5. **Step-2 screening-question widgets** — completely unknown. Generic
   base passes (native selects / radio groups / checkbox groups / text
   inputs) + ng-invalid check are the only coverage. Expect held/escalated
   questions. When escalated: read `pending/<case>/page.html`, identify the
   spl-* widget types, add targeted handlers (pattern: `handle_spl_consents`
   in this handler; `handle_yesno_buttons` in ashby.py is the model).
   Watch for NAMELESS checkbox inputs (invisible to base same-name
   grouping — same trap as Workday justfab; see `handle_wd_checkbox_groups`).
6. **Final submit button text** — `_submitish` accepts Submit / Submit
   Application / Apply / Apply now / Send application. If the real button
   says something else, add the exact phrase (NEVER loosen to an
   "apply" substring — partner buttons).
7. **Confirmation marker** — `_confirmed` phrases are guesses
   ("application submitted", "successfully submitted", "has been
   submitted", "thank you for applying", "thank you for your
   application"). Pin the real post-submit page (URL change? banner
   class?) and EXTEND the list; never re-add a loose thank-you+apply
   combo (that false-positives on the form page itself — see
   `test_smartrecruiters_confirmation_never_false_positives_on_form_page`).
8. **Wizard step count/shape** — `_step_signature` (URL + headings +
   visible field count) detects a bounced Next. If a step transition
   animates slowly, 2500ms may misread it as bounced → one harmless
   refill pass, then escalate. If that happens live, poll the signature
   for up to ~8s instead of the fixed wait.

## Shakeout runbook

1. Find a live posting (prefer a small company, not a dream job —
   first-run risk):
   - `https://api.smartrecruiters.com/v1/companies/<Company>/postings`
     lists live jobs (verify `active: true`).
   - jobs are also indexed on jobs.smartrecruiters.com; always confirm via
     the API before offering.
2. `supervised_mode: true` must be on (config.yaml — it is by default).
   Run: `python main.py <posting-url>`.
3. Watch step 1: identity ids fill, resume lands in the dropzone (NOT the
   avatar circle), city typeahead picks a suggestion, phone valid.
4. Click-through: let the handler press Next; at each escalate→pause,
   fix + rerun (escalations for step-2 unknowns are EXPECTED, not
   failures — each one is DOM intel; snapshot is in `pending/`).
5. At the supervised gate, review everything, then Enter. Record what the
   confirmation page shows before closing anything.
6. After the run: update `handlers/smartrecruiters.py` docstring,
   CLAUDE.md ("Proven live" + "SmartRecruiters specifics" + Known gaps),
   and add mock-DOM tests for every new widget handled. Run
   `python -m pytest tests/ -q` (no key/browser needed) — keep 137+ green.

## Invariants — do not regress

- ElementHandles only, never Locator-only methods (`press_sequentially`,
  `wait_for(`...) — a source-scan test enforces this.
- Resume upload stays dropzone-scoped (avatar input trap).
- `_submitish` stays exact-phrase + partner-name exclusion.
- `_confirmed` stays explicit-phrase only.
- Generic text pass never touches spl-autocomplete inner inputs
  (membership-based skip — role attributes are unverified).
- Twitter/Facebook URL inputs stay blank ("None" is for questions).
- No captcha/DataDome solving, ever; visible challenges escalate and are
  Tyler's to solve. Keep human pacing (`human_delay_ms`).
- Never invent profile facts; held > guessed.
- LinkedIn stays refused.
