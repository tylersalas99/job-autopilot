# Mac Setup Guide — job-autopilot

Follow these in order. Steps 1–4 are one-time setup (~15 min). Step 5 is the
dry-run test, 6 is the first live run, 7 adds phone control.

---

## 1. Unpack the project

✅ Done — the project lives at `~/Programs/job-autopilot`.

## 2. Install dependencies

Check Python (macOS ships with python3; 3.10+ needed):
```bash
python3 --version
```

Install Homebrew if you don't have it (needed for WeasyPrint's PDF libraries):
```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
brew install pango libffi        # PDF rendering libraries WeasyPrint needs
```

Project dependencies and the automation browser:
```bash
cd ~/Programs/job-autopilot
python3 -m venv .venv
source .venv/bin/activate        # do this in every new terminal before running
pip install -r requirements.txt
playwright install chromium
```

Sanity check (should print "33 passed"):
```bash
python -m pytest tests/ -q
```

## 3. Set your Anthropic API key

The tailoring step calls the Claude API. Create a key at
https://console.anthropic.com (Settings → API Keys), then:

```bash
echo 'export ANTHROPIC_API_KEY=sk-ant-YOUR-KEY-HERE' >> ~/.zshrc
source ~/.zshrc
```

Note: this API usage is billed separately from a claude.ai subscription —
each application costs roughly a few cents in API calls.

## 4. Verify your profile

```bash
open -e profile.yaml
```
Skim `standard_answers` — everything is filled in, but confirm the salary
range ($95k–$140k fallback), EEO answers, and start date still match reality.

## 5. Dry-run test (no submission, safe to repeat)

```bash
source .venv/bin/activate
python main.py "https://www.databricks.com/company/careers/integration-data-engineering-and-applications/software-engineer-web-products-8560779002" --dry-run
```

Expected: it fetches the JD, classifies the track, validates grounding, and
writes documents. Then:

```bash
open output/
```

Read `resume.pdf` and `cover_letter.txt`. Check: (a) it picked the SWE track,
(b) your Angular/web bullets lead, (c) nothing reads wrong or invented.
Dry-run 2–3 more real postings until you trust the output.

## 6. First live run (supervised)

Get a real Greenhouse **application** URL — click "Apply" on a posting and
copy the `boards.greenhouse.io/...` or `job-boards.greenhouse.io/...` URL, or
browse a company's board directly (e.g. boards.greenhouse.io/databricks).

```bash
python main.py "<greenhouse-application-url>"
```

A Chrome window opens and fills itself. Because `supervised_mode: true` (in
config.yaml), it PAUSES before clicking submit and waits for you to press
Enter in the terminal. Review every field in the browser, then Enter.

Do 5–10 supervised runs. When you trust it, edit config.yaml:
`supervised_mode: false` → fully hands-off. Leave `essay_policy: hold`.

## 7. Phone control via Claude Code Remote Control

One-time:
```bash
brew install node                # if you don't have Node.js
npm install -g @anthropic-ai/claude-code
cd ~/Programs/job-autopilot
claude                           # first launch: /login with your claude.ai account
```
(Requires a Claude Pro or Max subscription; check `claude --version` is
2.1.51+.) Accept the workspace-trust prompt, then exit.

To start a phone-controllable session:
```bash
cd ~/Programs/job-autopilot && source .venv/bin/activate
claude remote-control
```
Press spacebar to show the QR code and scan it with your phone — it opens the
session in the Claude app. From then on, texting the session a job URL makes
Claude Code on your Mac run the pipeline (CLAUDE.md tells it how).

Requirements while remote: Mac awake, terminal open. Prevent sleep with:
System Settings → Displays → Advanced → "Prevent automatic sleeping when the
display is off" (or run `caffeinate` in a spare terminal).

Heads-up for remote + supervised mode: the pre-submit pause waits for Enter
*in the terminal*. For phone-driven runs either flip `supervised_mode: false`
(after you trust it) or tell Claude Code to press Enter for you when you
approve from the phone.

---

## Daily use, once set up

At your desk:      python main.py "<url>"
From your phone:   message the Remote Control session the URL
Check history:     python main.py --status
Stuck items:       look in pending/ (screenshot + reason for each)
