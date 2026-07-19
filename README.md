# job-autopilot

Paste a job posting URL → tailored resume + cover letter + automated submission,
escalating to you only when something needs a human.

## Setup (one time)

```bash
cd job-autopilot
pip install -r requirements.txt
playwright install chromium          # downloads the browser Playwright drives

export ANTHROPIC_API_KEY=sk-ant-...  # from https://console.anthropic.com
                                     # (add to your shell profile to persist)
```

Then open **`profile.yaml`** and fill in every `TODO` under `standard_answers`
(work authorization, relocation, salary range, notice period, EEO choices).
Forms will ask these, and the system escalates rather than guesses when a
required answer is missing.

## Usage

```bash
python main.py <job-url>              # full pipeline
python main.py <job-url> --dry-run    # tailor documents only, no browser
python main.py --status               # counts by status
python main.py --export report.csv    # full tracking log
python main.py --batch urls.txt       # apply to a list of URLs (one per line, # = comment)
python main.py --pending              # list escalated cases + held essay drafts
python main.py <job-url> --answers pending/<case>/approved_answers.json
                                      # resume a held application with your
                                      # reviewed/edited answers
```

Generated documents land in `output/<company>_<title>/` as
`TylerSalas-Resume.pdf` (auto-fit to one page) and
`TylerSalas-CoverLetter.pdf`. Escalated cases land in
`pending/<timestamp>_<company>/` with a screenshot, the page HTML, and a
`case.json` explaining why.

**Workday** postings need a per-company account: the handler creates one
automatically with your profile email and a generated password, saved to
`workday_accounts.json` (gitignored — treat it like a password manager
export). If you already have an account at that company, the run escalates;
add your password to that file under the tenant host and re-run.

Workday runs print each wizard step as they go, and when something blocks
the run (an unmapped widget, a question held for review), it pauses in the
terminal instead of quitting: fix or fill whatever it names in the open
browser window, press Enter, and it picks up from the current page — your
manual answers are never overwritten. Applications resume Workday's saved
draft, so a re-run continues where the last one stopped. The supervised
gate still asks before the final Submit click.

## How review works during a run

With `essay_policy: review` (the default), the run pauses in the terminal at
every question it won't answer on its own — essays, low-confidence dropdown
picks, and unmapped required fields:

- **[Enter]** use the shown draft  **[e]** edit it in your $EDITOR
  **[t]** type a replacement  **[r]** ask Claude for a different draft
  **[s]** skip → escalate to `pending/`
- Dropdowns show a numbered option picker.
- With no terminal attached (phone-driven runs), review falls back to
  escalating with an editable `approved_answers.json` in the case folder —
  edit it and re-run with `--answers`; your answers are used verbatim.

After the supervised-mode Enter, if the run can't verify submission (e.g.
Greenhouse emails a one-time verification code on your first submission),
**the browser stays open** so you can finish by hand — then type `y` in the
terminal to record it as submitted. Verification usually sticks afterward
thanks to the persistent browser profile. The same keep-open prompt fires if
the handler crashes mid-run, with a snapshot saved to `pending/`.

An occasional **visible captcha at the gate is normal** — solve it yourself in
the open window. The persistent profile plus slow, human-paced form filling
(`human_delay_ms: [900, 2800]`) keeps these rare; they're most likely on your
first contact with a company's form.

## First runs — do this in order

1. **Dry-run 2–3 real postings** and read the tailored resume/cover letter.
   Verify the tailoring reads well and the track classification (SWE vs Data
   Eng) picks correctly.
2. **Supervised live runs** (`supervised_mode: true` in config.yaml, the
   default): the browser opens visibly, fills everything, then **pauses before
   the submit click** so you can review. Do 5–10 of these. ✅ Six submissions
   so far: Airtable + Smartsheet (Greenhouse), Supermove + Aledade (Lever),
   Close + Flipturn (Ashby). The Smartsheet run (2026-07-14) exercised the
   new job-boards UI end to end — education, school/location typeaheads,
   demographics, and "how did you hear" all filled hands-off. A same-day
   Ashby shakeout run verified the react widgets live: location combobox,
   Boolean Yes/No buttons, and the demographic standing answers.
3. Once trusted, set `supervised_mode: false` for hands-off submission.
   Keep `essay_policy: review` — it pauses only on questions the system
   won't answer on its own, which is where auto-generated answers carry
   real risk.
4. Before pointing `--batch` at a long list: add batch pacing (see What's
   next) — back-to-back submissions look bot-like. API rate-limit errors
   now retry with backoff instead of failing the application.

## What's built vs. what's next

| Piece | Status |
|---|---|
| Profile store (both resume tracks) | ✅ built from your two resumes |
| Intake, ATS detection, JD extraction | ✅ incl. embedded boards + company-from-URL fallback |
| Claude tailoring + anti-fabrication validation | ✅ skills sanitizer, numeric diff, fact-check with one feedback retry |
| PDF rendering | ✅ one-page auto-fit resume, name-based filenames, HTML fallback |
| Cover letter + form-answer voice | ✅ first-person enforced everywhere; form answers are pasted verbatim, so a deterministic guard catches "Tyler's profile..." / "listed in my profile" drafts, redrafts once, and holds for review if still wrong |
| Tracking DB, duplicate detection, CSV export | ✅ only real submissions block re-runs |
| Escalation queue + notifications (console/ntfy) | ✅ |
| **Greenhouse handler** | ✅ **live-proven** ×2 (classic + new job-boards React UI incl. education section, school/location async typeaheads) |
| **Lever handler** | ✅ **live-proven** ×2 (hidden-hcaptcha-button and /thanks-race quirks handled) |
| **Ashby handler** | ✅ **live-proven** ×2 + widget shakeout (React comboboxes — location typeaheads and static selects — plus Boolean Yes/No buttons, all live-verified 2026-07-14; Date pickers still escalate) |
| Dropdowns, radios, react-select comboboxes | ✅ profile-first, Claude fallback, review when unsure; radio groups hold when no question text is identifiable |
| Standing answers that never prompt | ✅ "how did you hear" always resolves to the careers page/site option (any wording, falls back to Other); ethnicity always Hispanic/Latino (variant-tolerant: Latino/Latinx/Latine); "communities you belong to" → None of the above; age 26 resolves range options like "25-34"; relocation, sponsorship, EEO, etc. from profile.yaml |
| Consent checkboxes (privacy/certify) | ✅ auto-ticked via strict regex; marketing opt-ins untouched |
| In-terminal review (`essay_policy: review`) | ✅ accept / edit / type / redraft / skip |
| Resume-with-answers (`--answers`) | ✅ for phone-driven or escalated runs |
| Batch mode (`--batch urls.txt`) | ✅ (add pacing before heavy use) |
| Pending-case viewer (`--pending`) | ✅ |
| API retry/backoff | ✅ exponential backoff + jitter on 429/5xx/connection errors, honors Retry-After (`anthropic.max_retries`, default 5) |
| Workday handler | ⬜ (biggest lift) |
| Ashby Date pickers ("Pick date..." calendar inputs) | ⬜ skipped by the fill loop, escalate via the required check |
| Confirmation-email verification (IMAP) | ⬜ optional |

## Design guarantees

- **No fabrication:** every tailored bullet must trace to a bullet id in
  `profile.yaml`; numeric claims are diffed against the source, and a second
  Claude pass fact-checks the rest. Failures escalate instead of submitting.
- **LinkedIn is refused** by policy (account-suspension risk).
- **CAPTCHAs escalate** — never solved automatically. (Invisible reCAPTCHA
  plumbing, present on nearly every Greenhouse form, is correctly ignored —
  only a visible challenge escalates.)
- **Nothing silently fails:** every attempt is logged; every stall produces a
  `pending/` case with a screenshot.

## Testing

```bash
python -m pytest tests/ -q    # 79 tests, no API key or browser needed
```

## Notes

- The persistent Chrome profile lives in `browser_profile/` — log in to portals
  there once and sessions persist. Treat the folder as sensitive.
- Run locally, not on a server: residential IP + real browser profile is your
  best bot-detection mitigation.
- API docs, if you want to tweak the tailoring calls:
  https://docs.claude.com/en/api/overview
