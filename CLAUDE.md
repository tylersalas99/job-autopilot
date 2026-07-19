# job-autopilot — Claude Code operating instructions

This project applies to jobs automatically. When the user sends a job posting
URL (often from their phone via Remote Control), run the pipeline for them.

## Primary workflow

When the user provides a job URL with intent to apply:

```bash
python main.py <url>
```

- If they say "dry run", "test", or "just the documents": add `--dry-run`.
- Report the outcome concisely: status, company, title, and where documents
  landed. If escalated, read `pending/<case>/case.json` and explain the reason
  and what the user should do.

## Proven live

Six submissions as of 2026-07-14: Airtable + Smartsheet (Greenhouse),
Supermove + Aledade (Lever), Close + Flipturn (Ashby). The Aledade Lever run
was fully clean end-to-end; Smartsheet (new job-boards React UI) submitted
clean on 2026-07-14 after shakeout — education typeaheads, location,
demographics, and hear-about all hands-off. Ashby's react widgets
(location combobox, Boolean yes/no buttons) were verified live in a
supervised shakeout run on 2026-07-14. Workday was shaken out 2026-07-18
against Red Hat and Travelers — full wizard reached on both, but no clean
unattended submission yet (see "Workday specifics"). All handlers are past
their known shakeout bugs — see "Behavior worth knowing" for the classes
of failure already handled.

## Finding live postings for the user

- Search-engine results for ATS postings are often stale — verify before
  offering. Lever posting pages are server-rendered: an empty web fetch
  means the posting is gone.
- Ashby pages are JS-rendered and can't be checked by fetch, but
  `https://api.ashbyhq.com/posting-api/job-board/<company-slug>` returns
  JSON of all currently listed jobs (title, location, jobUrl, isListed) —
  use it to confirm liveness and discover other openings at that company.

## Key facts

- Personal data and standard form answers: `profile.yaml` (single source of
  truth — tailoring must never invent facts not in it).
- Settings: `config.yaml`. `supervised_mode: true` pauses before the submit
  click (the run waits for Enter in the terminal — if the user is remote, tell
  them and press Enter on their confirmation). `essay_policy: review` pauses
  in-terminal at each held question for accept/edit; it automatically falls
  back to escalation when there is no interactive terminal (phone runs), so
  remote runs land in `pending/` — resume them with `--answers`.
- Tailored documents: `output/<company>_<title>/`
- Escalated cases: `pending/` (each has case.json, screenshot.png, page.html)
- Tracking: `python main.py --status` or `--export report.csv`
- Batch: `python main.py --batch urls.txt` (one URL per line; failures are isolated)
- Pending cases: `python main.py --pending` lists escalations and held essay drafts
- Resuming held essays: each held case has an editable `approved_answers.json`.
  Help the user review/edit the drafts (never approve unreviewed drafts
  yourself), then re-run: `python main.py <url> --answers <case>/approved_answers.json`
- Tests: `python -m pytest tests/ -q` (no API key or browser needed)

## Behavior worth knowing before debugging

- **Form coverage (Greenhouse):** identity inputs, resume + cover letter
  upload (matched by name OR id), textareas AND single-line question inputs
  (labels >60 chars are treated as questions, short labels as identity
  fields), native `<select>`, react-select comboboxes (options are scoped to
  the opened control — a page-wide [role=option] query grabs the phone
  widget's country list), and radio groups. Choice questions resolve
  profile-first (`choice_value` in handlers/base.py), then Claude, then
  in-terminal review.
- **CAPTCHA detection is visibility-based.** Invisible reCAPTCHA
  infrastructure is on every Greenhouse form and does NOT block submission;
  only visible checkboxes/challenges escalate. Do not "fix" detection by
  matching on page text.
- **After submit, Greenhouse may email a verification code** (first
  submission from a fresh browser profile). Runs that don't verify keep the
  browser open and ask the user; typing `y` records a manual submission. The
  persistent `browser_profile/` means verification usually sticks.
- **Any run that ends unsubmitted — including handler crashes — keeps the
  browser open** and offers the y/Enter prompt; crashes also snapshot to
  `pending/`. Never let a fix regress this (main.py wraps handler.apply).
- **Submit buttons are selected visible-first** (`_visible` in base.py):
  Lever ships a hidden `#hcaptchaSubmitBtn[type=submit]` earlier in the DOM
  that times out if clicked. Lever's real submit click can also race the
  `/thanks` navigation (user may even submit manually at the supervised
  gate) — the handler re-checks the URL before declaring failure.
- **Confirmation markers vary by ATS:** Lever navigates to `/thanks`;
  Ashby shows a `success-container` banner saying "successfully submitted"
  (URL unchanged). Both checked; extend rather than replace these.
- **Occasional visible captchas at submit are expected and are the user's
  to solve** (supervised gate / keep-open window). `human_delay_ms` is
  [900, 2800] deliberately — slower pacing lowers risk-based challenges.
  Never add captcha-solving of any kind.
- **Duplicate guard blocks only `submitted` records** — dry runs (status
  `dry_run`), held, and escalated attempts are always re-runnable.
- **Cover letter structure is template-controlled:** header, single
  'Dear Hiring Manager,', model-written body (first person, enforced by a
  third-person detector that auto-redrafts once), then 'Sincerely, Tyler
  Salas'. Greetings/sign-offs the model emits are stripped deterministically.
  Don't add them back via the prompt.
- **Form answers are pasted VERBATIM, so voice is enforced** (user
  2026-07-14): `draft_field_answer` drafts must be first person and never
  mention the profile/resume/mechanics ("Tyler's profile is...", "listed
  in my profile"). `sounds_wrong_voice` in tailoring/resume.py catches
  violations deterministically, triggers one redraft, and forces
  confidence "low" (→ held for review) if the redraft is still wrong.
- **Resume renders one page** (`fit_one_page=True` shrinks fonts to 88% max)
  and is named `TylerSalas-Resume.pdf`; cover letter is
  `TylerSalas-CoverLetter.pdf`. Recruiters see these filenames.
- **Skills can't be invented:** `sanitize_skills` filters every tailored
  skill item back to profile-verbatim text. Failed fact-checks retry once
  with the problems as feedback before escalating.
- **Greenhouse redirects tell you things:** /jobs/<id> → board index with
  `?error=true` means the posting is CLOSED (intake detects this and stops
  before tailoring, status `posting_closed`). A redirect to the company's
  OWN domain means a JS-embedded board (div#grnhse_app) — intake rewrites
  final_url to `job-boards.greenhouse.io/embed/job_app?for=<slug>&token=<id>`
  so the handler reaches the real form.
- **Education section (Greenhouse job-boards UI) auto-fills** from
  `profile.education[0]`: school (async typeahead — types the name, picks
  the match), degree (via `degree_candidates` — option lists vary between
  "Bachelor of Science" and "Bachelor's Degree" style), discipline (text
  after "in" in the degree string), and start/end years (from `started`/
  `graduated`). Unmatched pieces are left alone, never guessed.
- **"Location (City)" (`candidate-location`) is a required react-select
  typeahead** — the handler types the profile city and picks the suggestion.
  School and location pickers use `fuzzy_index` (significant-token match,
  ≥2 tokens) because option text reformats names ("University Texas -
  El Paso", "El Paso, Texas, United States"). Fuzzy matching is opt-in for
  these pickers ONLY — never for yes/no or generic choice questions.
- **Label `for` attributes can be NUMERIC** ("326" on demographic
  questions) — `#326` is invalid CSS; always select via `[id='...']`.
  Those numeric-id fields are react-select comboboxes: `_fill_labeled_inputs`
  must never type into `role=combobox` inner inputs.
- **Never fill text into input[type=number]** — Playwright throws instantly.
  The education section's "Start date year"/"End date year" number inputs
  must not match the earliest-start-date mapping (year/month lookahead in
  `standard_value`), and `_fill_labeled_inputs` skips numeric inputs for
  non-numeric values.
- **Checkbox GROUPS (≥2 same-name) are choice questions, not consent boxes**
  — e.g. Lever's standard pronouns widget (10 checkboxes named `pronouns`
  plus a hidden "Custom" text field that must never be fill()ed: invisible
  text inputs are skipped in the custom-question loop). Groups resolve
  profile-first (`standard_answers.pronouns` if set), then Claude; exactly
  one option is checked. Singleton checkboxes stay with the consent handler.
- **Radio-group questions are extracted by excluding option labels** — if no
  question text can be identified (or it equals an option), the group is HELD,
  never guessed. Consent/agreement checkboxes (privacy policy, "I certify")
  are auto-ticked via a strict regex; marketing opt-in checkboxes are not.
- **Standing answers:** non-compete: No; hiring-related SMS opt-in: Yes;
  transgender: No (the `transgender` row in `choice_value` MUST stay above
  the `gender` row — "transgender" contains "gender"); "how did you hear":
  "Company careers page" for both choice AND free-text variants — and since
  choice option text varies per company ("Smartsheet Careers Site"),
  `careers_option_index` in base.py resolves hear-about dropdowns by
  preference (careers page/site → company website → careers-ish but never
  "Career Fair" → "Other") so this question NEVER prompts or holds
  (user 2026-07-14). The fallback fires only for hear-about questions.
  Ethnicity questions ALWAYS answer Hispanic/Latino (user 2026-07-14) —
  option wording varies ("Hispanic or Latine", "Latinx"), so
  `ethnicity_option_index` in base.py variant-matches when exact matching
  fails (a miss once fell through to Claude, which picked "I prefer not
  to answer"). "Which communities do you belong to?" surveys answer
  "None of the above" (`demographic_communities` in profile.yaml).
  Age is 26 (`standard_answers.age`); range options ("25-34") resolve via
  `age_option_index`, and the age row sits BELOW the over-18 row so
  "...18 years of age?" keeps answering Yes.
  Twitter/X URL questions answer "None"; portfolio/
  website questions answer with the GitHub URL (both in profile.yaml
  identity). Free-text relocation questions always answer with
  `standard_answers.relocation_answer` ("...willing to relocate"); choice
  relocation questions answer Yes. The profile email tylersalas66@gmail.com
  is correct (deliberately different from the user's Claude account email).
- **School pickers type each `school_aliases` entry as a separate search**
  when the canonical name returns no results — Greenhouse's school DB
  entry is "University of Texas - El Paso" and finds nothing for "The
  University of Texas at El Paso". After the verbatim queries, a
  distinctive-tail query (last two significant tokens, "el paso") is typed
  as a last resort, with every candidate matched (exact, then fuzzy)
  against each result set. `_poll_match` waits up to 6s for async options —
  a fixed wait races the backend search and loses (that cost one Smartsheet
  run); a visible "No options" notice ends a query's wait early.
- **Ashby widgets (see handlers/ashby.py docstring for the full DOM
  contract):** comboboxes are `input[role=combobox]` whose options portal
  to a `[role=listbox]` on `<body>` (page-wide option query is CORRECT
  here — one menu open at a time) and whose chosen value commits into
  `input.value` (that's the answered check). Location comboboxes are async
  typeaheads (type city/country, poll like the school picker); long
  ValueSelects open a static list on click; SHORT ValueSelects and EEO
  render as native radios. Boolean questions are Yes/No BUTTON pairs
  (`[class*='_yesno']`) — answered = `_active` class on a button; the
  hidden mirror checkbox's `checked` is the VALUE (false for "No"), never
  use it as the answered signal, and never let the consent handler tick
  it. Ashby marks required via a `_required_` class on the entry's label,
  NOT aria-required — the required-empty check covers widgets separately.
- **choice_value's country row is label-style-only** (`"Country*"`,
  "Country of residence") — a bare `\bcountry\b` matched "authorized to
  work in the COUNTRY where the job is located?" (Ashby Boolean) and
  shadowed the authorization row (2026-07-14).
- **Handlers hold ElementHandles (query_selector), never Locators.**
  Locator-only methods (press_sequentially etc.) raise AttributeError,
  which the handlers' broad except blocks swallow — the field is silently
  skipped (this killed location AND school in one run). Type into
  typeaheads keystroke-by-keystroke with `ElementHandle.type`, not `fill`;
  a source-scan test enforces the API boundary.

## Known gaps (next work, in order)

1. First CLEAN unattended Workday submission — both shakeout applications
   (Red Hat, Travelers, 2026-07-18) reached the end of the wizard but
   needed manual nudges for bugs that are all since fixed. The Self
   Identify page (signature date + disability trio) and the repaired
   hear-about/date/veteran paths have NOT been re-verified live. Supervise
   the next Workday run end-to-end.
2. Batch pacing — add a delay between batch submissions before real use.
3. Ashby Date fields ("Pick date..." calendar inputs) — skipped by the fill
   loop (fill() text is ignored by React), escalate via the required check.
4. Optional: IMAP confirmation-email verification.

(Done 2026-07-18: Workday handler — handlers/workday.py, full DOM contract
in its docstring, shaken out live against Red Hat (wd5) and Travelers (wd5)
through the entire wizard. Per-tenant accounts are auto-created with the
profile email and a generated password in workday_accounts.json (gitignored,
chmod 600); existing-account collisions escalate asking for the password
rather than guessing. Flow: Apply → Autofill with Resume (user 2026-07-18) →
auth → wizard pages, each filled via the shared profile-first pipeline plus
Workday widgets. Validation errors (banner OR field-level) re-run the fill
passes once, then escalate with Workday's own error text. Intake enriches
Workday postings through the public CXS JSON endpoint (workday_api_url in
intake/fetcher.py) since job pages are JS-rendered; the same endpoint is
how to verify a Workday posting is live before offering it.)

(Done 2026-07-14: API retry/backoff — `_create` in tailoring/resume.py wraps
every messages.create call with exponential backoff + jitter on
429/5xx/connection errors and honors Retry-After. SDK retries are off
(`max_retries=0` on the client) so behavior is deterministic. Config knob:
`anthropic.max_retries`, default 5.)

(Done 2026-07-14: Ashby react comboboxes + Boolean yes/no buttons —
`handle_comboboxes` and `handle_yesno_buttons` in handlers/ashby.py; DOM
contract documented in that file's docstring, captured live from
OpenAI/Ashby/ElevenLabs boards. Mock-DOM tests cover both; verified in a
live supervised run 2026-07-14. Same session: verbatim-voice guard on
form answers, ethnicity/communities/age standing answers.)

## Supported ATS

Greenhouse, Lever, Ashby, and Workday have handlers (see
`handlers/registry.py`). Greenhouse/Lever/Ashby are live-proven, including
Ashby's combobox/Boolean widgets (added and live-verified 2026-07-14).
Workday (added + shaken out on Red Hat and Travelers 2026-07-18) walks the
full wizard but hasn't yet produced a clean unattended submission —
supervise it. Lever/Ashby intake derives the company from the URL slug and
strips it from titles; Workday intake pulls title/company/description from
the tenant's CXS JSON endpoint (job pages are JS-rendered) and falls back
to the tenant slug for the company.
LinkedIn URLs are refused by policy — never bypass this.

## Workday specifics

All findings below came from the live Red Hat + Travelers shakeouts
(2026-07-18); each is fixed and pinned by a test. The full widget/DOM
contract lives in handlers/workday.py's docstring — data-automation-id
driven (Workday's CSS classes are obfuscated/unstable).

- **Credentials:** per-tenant in `workday_accounts.json` (auto-created,
  gitignored, chmod 600). Never commit it or read passwords aloud. The
  generated password (16 chars; policy floor seen live: 14) is saved
  BEFORE the create click, so a failed creation never loses it — sign-in
  with a stored credential falls back to creating the account with that
  same password. "Account already exists" escalates: ask Tyler for the
  password, add it under the host key, re-run.
- **Escalations pause instead of ending** (terminal runs): the case is
  written, then [Enter] re-enters the wizard from the current page after
  you fix things in the open browser; [s] stops. Phone/no-TTY runs
  escalate and stop. Held questions print at the pause, never
  auto-answered. Loop guard: ONE repeat pass per page, then escalate.
- **Auth:** submit buttons hide under a `click_filter` overlay that
  intercepts pointer events — always click the overlay. The auth page
  ALSO renders the progress bar (its step 1), so a visible password field
  means auth, never wizard. Never fill the `beecatcher` honeypot input.
- **Async rendering is the #1 bug class:** footer renders before form
  fields — wait for the formField count to stabilize before filling;
  wizard steps are read from `progressBarActiveStep` (h1/h2 is the job
  title, identical everywhere); check step-change BEFORE the error banner
  (validation can arrive on the NEXT page); field-level `errorMessage`
  nodes without a banner still mean "retry the fill", not "stuck".
- **Widgets:** field inputs carry NO automation ids — the `formField-*`
  wrappers do. Selected multiselect pills reuse `promptOption` (filter
  anything under `selectedItemList` or clicking "an option" DESELECTS the
  answer). Option clicks re-query by text with 5s timeouts and fall into
  held on failure. Radios: click `label[for=…]`, never `input.check()`.
  Questionnaires may be button-dropdowns (Red Hat) OR native <select>s
  (Travelers) — both passes run. Date spinbuttons: write via the native
  value setter + input event (typing gets swallowed by the calendar
  popup), verified, keyboard fallback.
- **Resume-parse lies:** Workday's Autofill invents work/education dates
  (Lugo became 01/2008–01/2019). `verify_experience_dates` rewrites them
  from profile ground truth, matching panels by company; non-profile
  employers and the current job's end date are never touched.
- **Standing answers proven on Workday:** ever-worked-for/currently-
  employed-by <company> → No (unless the company is in work_history —
  then held); hold/require work authorization → No, plain authorized-to-
  work → Yes; veteran → always "not a veteran" (`veteran_option_index`
  runs BEFORE containment — "a veteran, but I am not a protected veteran"
  CONTAINS the standing answer); ethnicity ignores negated "(Not Hispanic
  or Latino)" mentions; hear-about matches "<Company> Jobs Site";
  "certify …true and accurate" dropdowns → Yes; involuntarily
  discharged/asked to resign → No; `phone-sms-opt-in` from sms_opt_in;
  state dropdowns want the full name (`identity.state_full`).

## When the automation breaks

If a run fails on a page structure the handler doesn't understand:
1. Read the escalation case (screenshot + page.html) to see what happened.
2. Fix the selector/logic in the relevant handler.
3. Re-run the same URL to verify.
4. Tell the user what changed.

## Hard rules — do not override

- Never fabricate resume content; the validation pass in
  `tailoring/resume.py` must stay intact.
- Never attempt to solve or bypass CAPTCHAs.
- Never automate LinkedIn.
- Never submit if grounding validation fails — escalate instead.
- Do not change `profile.yaml` answers without the user's explicit direction.
