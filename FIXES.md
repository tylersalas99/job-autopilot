# Fixes applied — 2026-07-12 (Cowork debugging session)

## Bugs fixed

1. **Paths resolved against CWD, not project root** (`main.py`)
   `pending/`, `browser_profile/`, and `output/` were passed to handlers as
   relative paths, so running `python main.py` from any directory other than
   the project root would scatter folders into the current directory. New
   `load_config()` anchors every path in `config.yaml` to the project root.

2. **Duplicate detection missed redirected URLs** (`tracking/log.py`)
   `duplicate()` only compared against the `url` column, but is called with
   the *final* (post-redirect) URL. A shortlink and its destination were not
   recognized as the same application. Query now checks `url OR final_url`.
   Regression test added (`test_tracker_duplicate_matches_final_url`).

3. **Potential KeyError on missing `exclusion_rules`** (`main.py`)
   `profile["exclusion_rules"]` → `profile.get("exclusion_rules", {})`.

4. **Brittle JSON parsing of Claude replies** (`tailoring/resume.py`)
   If the model wrapped its JSON in prose, `json.loads` crashed the run.
   `_ask_json` now falls back to extracting the outermost `{...}` object.

5. **Doc drift**: README/SETUP_MAC said "11 tests"; suite is now 15.

## Verified working

- All 15 tests pass (Python 3.10, Linux).
- PDF rendering via weasyprint produces real PDFs (resume + cover letter).
- ATS detection correct for greenhouse / lever / ashby / workday / linkedin.
- `--status`, tracker roundtrip, escalation cases all work.
- `claude-sonnet-4-6` confirmed as a valid current API model ID.

## Flagged for your confirmation (not changed)

- `profile.yaml` email is **tylersalas66@gmail.com** — your Claude account is
  tylersalas8@gmail.com. Confirm which inbox should receive application
  confirmations before live runs.

## Dropdown/radio handling — added 2026-07-12

New in `handlers/base.py`: `choice_value()` (deterministic label →
standard_answers mapping for sponsorship, work auth, relocation, veteran,
disability, gender, race/ethnicity, Hispanic/Latino, over-18, how-did-you-hear,
criminal history, citizenship), `match_option()` (safe option matching —
bare Yes/No never substring-matches), `answer_choice()` (profile first,
Claude fallback via new `draft_choice_answer()` in tailoring/resume.py,
holds on low confidence), `handle_native_selects()` (incl. hidden selects
behind JS widgets like select2 — sets value + fires input/change), and
`handle_radio_groups()`. Wired into all three handlers; Greenhouse's
required-field check now also covers `<select>`. Ashby's React comboboxes
still escalate (native selects/radios are handled when present).

Verified offline with a mock-DOM harness: EEO selects fill with the correct
option values, radio groups answer work-auth correctly, unknown questions
hold for review (no crash even without an API key). 19 tests pass.

## Suggested next improvements (in priority order)

1. **Greenhouse JSON API for intake** — `boards-api.greenhouse.io/v1/boards/
   <company>/jobs/<id>` returns clean title/company/description; the new
   job-boards.greenhouse.io pages are React-rendered and scrape thin.
   Same for Lever (`api.lever.co/v0/postings/...`) and Ashby's posting API.

2. **API retry/backoff** — one 429/529 from the Anthropic API currently kills
   a run mid-application. Wrap `_ask_json` calls with 2–3 retries.

3. **Confirmation-email check (IMAP)** — closes the loop on "submission
   unverified" escalations.

4. **Workday handler** — biggest remaining ATS gap (multi-page + account
   creation; substantial work).

5. **Batch pacing** — add a configurable sleep (e.g. 2–5 min) between batch
   submissions to look less bot-like across a session.
