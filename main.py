#!/usr/bin/env python3
"""job-autopilot — paste a job posting URL, get a submitted application.

Usage:
    python main.py <url>                 # full pipeline
    python main.py <url> --dry-run       # everything except browser submission
    python main.py --status              # tracking summary
    python main.py --export report.csv   # export tracking log
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

import yaml

from escalation.queue import escalate, notify
from intake.fetcher import BLOCKED_ATS, SUPPORTED_ATS, check_exclusions, fetch_posting
from tailoring import resume as tailor
from tailoring.render import cover_letter_html, render_pdf, resume_html
from tracking.log import Tracker

ROOT = Path(__file__).parent


def load_yaml(name: str) -> dict:
    return yaml.safe_load((ROOT / name).read_text(encoding="utf-8"))


def load_config() -> dict:
    """Load config.yaml with all paths anchored to the project root.

    Handlers receive cfg and resolve paths directly; without this, running
    main.py from another directory would scatter pending/ and
    browser_profile/ into the current working directory.
    """
    cfg = load_yaml("config.yaml")
    cfg["paths"] = {k: str((ROOT / v).resolve()) for k, v in cfg["paths"].items()}
    return cfg


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "unknown").lower()).strip("-")[:50]


# Run folders are '<NNN>_<MMDDYYYY>_<company>_<title>' (user 2026-07-21):
# a zero-padded sequential id, the run date, then the readable posting slug.
_RUN_DIR_RE = re.compile(r"^(\d{3,})_(\d{8})_(.*)$")


def application_dir(output_root: Path, company: str, title: str) -> Path:
    """Resolve the output folder for a posting.

    Re-running the SAME posting reuses its existing folder (idempotent — the
    id and date stay pinned to the first run, and documents overwrite in
    place, exactly like the old '<company>_<title>' scheme). A NEW posting
    gets the next sequential id across the whole output dir and today's date.
    """
    output_root = Path(output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    base = f"{slug(company)}_{slug(title)}"
    max_id = 0
    for d in sorted(output_root.iterdir()):
        if not d.is_dir():
            continue
        m = _RUN_DIR_RE.match(d.name)
        if m:
            max_id = max(max_id, int(m.group(1)))
            if m.group(3) == base:
                return d  # this posting already has a folder — reuse it
        elif d.name == base:
            return d  # legacy un-prefixed folder (pre-migration) — reuse as-is
    stamp = datetime.now().strftime("%m%d%Y")
    return output_root / f"{max_id + 1:03d}_{stamp}_{base}"


def check_profile_todos(profile: dict) -> list[str]:
    todos = []
    for key, val in profile["standard_answers"].items():
        if isinstance(val, str) and val.startswith("TODO"):
            todos.append(key)
    return todos


def run(url: str, dry_run: bool, answers_file: str | None = None) -> int:
    cfg = load_config()
    profile = load_yaml("profile.yaml")
    approved_answers = {}
    if answers_file:
        approved_answers = json.loads(Path(answers_file).read_text(encoding="utf-8"))
        print(f"→ Loaded {len(approved_answers)} approved answer(s) from {answers_file}")
    tracker = Tracker(ROOT / cfg["paths"]["db_path"])

    todos = check_profile_todos(profile)
    if todos and not dry_run:
        print(f"⚠️  profile.yaml still has TODO answers: {', '.join(todos)}")
        print("   Fill these in before live submissions (forms may need them). Continuing...")

    # 1. Intake
    print(f"→ Fetching {url}")
    posting = fetch_posting(url)
    print(f"  ATS: {posting.ats} | {posting.title or '(no title)'} @ {posting.company or '(unknown company)'}")
    for w in posting.warnings:
        print(f"  ⚠️  {w}")

    if posting.closed:
        tracker.record(company=posting.company, title=posting.title, url=url,
                       final_url=posting.final_url, ats=posting.ats,
                       status="posting_closed", reason="Posting no longer live")
        print("✗ Posting is no longer live — the ATS redirected to the board index.")
        return 1

    if posting.ats in BLOCKED_ATS:
        tracker.record(company=posting.company, title=posting.title, url=url,
                       final_url=posting.final_url, ats=posting.ats,
                       status="rejected_by_rules", reason="LinkedIn excluded by policy")
        print("✗ LinkedIn is excluded — locate the posting on the company's own careers site.")
        return 1

    # 2. Exclusion rules + duplicate check
    reason = check_exclusions(posting, profile.get("exclusion_rules", {}))
    if reason:
        tracker.record(company=posting.company, title=posting.title, url=url,
                       final_url=posting.final_url, ats=posting.ats,
                       status="rejected_by_rules", reason=reason)
        print(f"✗ Skipped: {reason}")
        return 1
    dup = tracker.duplicate(posting.company, posting.title, posting.final_url,
                            profile.get("exclusion_rules", {}).get("skip_if_applied_within_days", 90))
    if dup:
        tracker.record(company=posting.company, title=posting.title, url=url,
                       final_url=posting.final_url, ats=posting.ats,
                       status="duplicate", reason=f"Already handled on {dup['created_at']}")
        print(f"✗ Duplicate — already applied {dup['created_at']}")
        return 1

    # 3. Tailoring
    # A blank/thin JD means everything downstream (track, resume tailoring,
    # cover letter) is ungrounded — the drafter even returns a refusal string.
    # Halt here instead of generating and uploading garbage documents.
    if len((posting.description or "").strip()) < 200:
        case = escalate(ROOT / cfg["paths"]["pending_dir"],
                        reason="Job description missing/thin — cannot tailor documents",
                        url=posting.final_url, company=posting.company, title=posting.title,
                        extra={"warnings": posting.warnings})
        tracker.record(company=posting.company, title=posting.title, url=url,
                       final_url=posting.final_url, ats=posting.ats,
                       status="escalated", reason="Job description missing/thin")
        print(f"✗ Job description missing/thin → {case}")
        return 1

    app_dir = application_dir(ROOT / cfg["paths"]["output_dir"],
                              posting.company, posting.title)
    app_dir.mkdir(parents=True, exist_ok=True)
    (app_dir / "jd.txt").write_text(
        f"{posting.title}\n{posting.company}\n{posting.final_url}\n\n{posting.description}",
        encoding="utf-8")

    track = tailor.classify_track(cfg, profile, posting.description, posting.title)
    print(f"→ Track: {profile['tracks'][track]['label']}")

    tailored = tailor.tailor_resume(cfg, profile, track, posting.description,
                                    posting.title, posting.company)
    problems = tailor.validate_tailored(cfg, profile, tailored)
    if problems:
        print(f"⚠️  Fact-check flagged {len(problems)} problem(s) — retrying with feedback")
        tailored = tailor.tailor_resume(cfg, profile, track, posting.description,
                                        posting.title, posting.company, feedback=problems)
        problems = tailor.validate_tailored(cfg, profile, tailored)
    if problems:
        case = escalate(ROOT / cfg["paths"]["pending_dir"],
                        reason="Tailored resume failed grounding validation",
                        url=posting.final_url, company=posting.company, title=posting.title,
                        extra={"problems": problems, "tailored": tailored})
        tracker.record(company=posting.company, title=posting.title, url=url,
                       final_url=posting.final_url, ats=posting.ats, track=track,
                       status="escalated", reason="; ".join(problems)[:500])
        print(f"✗ Validation failed → {case}\n  " + "\n  ".join(problems))
        return 1
    print("✓ Grounding validation passed")

    # Recruiters see the uploaded filename — use the candidate's name, not "resume.pdf"
    resume_name = f"{profile['identity']['full_name'].replace(' ', '')}-Resume.pdf"
    resume_path = render_pdf(resume_html(profile, tailored, track), app_dir / resume_name,
                             fit_one_page=True)
    letter = tailor.draft_cover_letter(cfg, profile, track, posting.description,
                                       posting.title, posting.company)
    (app_dir / "cover_letter.txt").write_text(letter, encoding="utf-8")
    # Guard against a model refusal/clarification getting rendered into the PDF
    # a recruiter would see (e.g. "I'll need the actual job description...").
    _bad_letter = (
        len(letter.strip()) < 200
        or re.search(r"\b(I'll need|I need|could you (paste|provide|share)|"
                     r"please (paste|provide|share)|as an ai|I('m| am) unable"
                     r"|actual job description)\b", letter, re.IGNORECASE))
    if _bad_letter:
        case = escalate(ROOT / cfg["paths"]["pending_dir"],
                        reason="Cover letter draft looks invalid (possible refusal) — review",
                        url=posting.final_url, company=posting.company, title=posting.title,
                        extra={"cover_letter_draft": letter})
        tracker.record(company=posting.company, title=posting.title, url=url,
                       final_url=posting.final_url, ats=posting.ats, track=track,
                       status="escalated", reason="Invalid cover letter draft")
        print(f"✗ Cover letter draft invalid → {case}")
        return 1
    letter_name = f"{profile['identity']['full_name'].replace(' ', '')}-CoverLetter.pdf"
    letter_path = render_pdf(cover_letter_html(profile, letter), app_dir / letter_name)
    (app_dir / "tailored.json").write_text(json.dumps(tailored, indent=2), encoding="utf-8")
    print(f"✓ Documents → {app_dir}")

    app_id = tracker.record(company=posting.company, title=posting.title, url=url,
                            final_url=posting.final_url, ats=posting.ats, track=track,
                            status="dry_run" if dry_run else "held",
                            reason="documents generated",
                            resume_path=str(resume_path), cover_letter_path=str(letter_path),
                            jd_snapshot_path=str(app_dir / "jd.txt"))

    if dry_run:
        print("✓ Dry run complete — no browser automation performed.")
        return 0

    # 4. Form automation
    if posting.ats not in SUPPORTED_ATS:
        case = escalate(ROOT / cfg["paths"]["pending_dir"],
                        reason=f"No handler for ATS '{posting.ats}' yet — apply manually with generated docs",
                        url=posting.final_url, company=posting.company, title=posting.title,
                        extra={"documents_dir": str(app_dir)})
        tracker.update_status(app_id, "escalated", f"unsupported ATS: {posting.ats}")
        notify(cfg, f"Manual apply needed ({posting.ats}): {posting.title} @ {posting.company}")
        print(f"→ Escalated (no {posting.ats} handler yet). Docs ready in {app_dir}")
        return 0

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("✗ Playwright not installed. Run:\n"
              "    pip install playwright && playwright install chromium")
        tracker.update_status(app_id, "escalated", "playwright not installed")
        return 1

    from handlers.registry import get_handler
    handler_cls = get_handler(posting.ats)
    handler = handler_cls(cfg, profile, posting,
                          {"resume": resume_path, "cover_letter": letter_path,
                           "cover_letter_text": letter,
                           "jd_text": posting.description, "track": track,
                           "approved_answers": approved_answers},
                          tracker)
    with sync_playwright() as p:
        context = handler.launch(p)
        page = context.new_page()
        try:
            try:
                result = handler.apply(page)
            except Exception as exc:  # a crash must never slam the browser shut
                import traceback
                print(f"\n✗ Handler crashed: {type(exc).__name__}: {exc}")
                traceback.print_exc()
                # Snapshot the page into pending/ and fall through to the
                # keep-open prompt so the user can inspect/finish manually.
                result = handler.escalate_now(
                    page, f"handler crashed: {type(exc).__name__}: {str(exc)[:400]}")
            # If the run didn't end in a verified submission, keep the browser
            # open so the user can intervene (email verification codes,
            # captchas after submit, half-filled forms) before we tear it down.
            if result.status != "submitted" and sys.stdin.isatty():
                print(f"\n⏸  Run ended: {result.status} — {result.reason}")
                print("   The browser window is STILL OPEN. Finish or fix things "
                      "manually there if you can.")
                try:
                    done = input("   Type 'y' if you completed the submission "
                                 "manually, or press Enter to close the browser: ").strip().lower()
                    if done == "y":
                        result.status = "submitted"
                        result.reason = f"completed manually ({result.reason})"
                except (KeyboardInterrupt, EOFError):
                    pass
        finally:
            context.close()

    tracker.update_status(app_id, result.status, result.reason)
    icon = {"submitted": "✅", "held": "⏸", "escalated": "🔔", "failed": "✗"}.get(result.status, "•")
    print(f"{icon} {result.status.upper()}: {result.reason}")
    return 0 if result.status == "submitted" else 1


def run_batch(batch_file: str, dry_run: bool) -> int:
    urls = [ln.strip() for ln in Path(batch_file).read_text().splitlines()
            if ln.strip() and not ln.strip().startswith("#")]
    print(f"Batch: {len(urls)} URLs\n")
    results = {}
    for i, url in enumerate(urls, 1):
        print(f"\n===== [{i}/{len(urls)}] {url} =====")
        try:
            rc = run(url, dry_run)
            results[url] = "ok" if rc == 0 else "skipped/escalated"
        except Exception as exc:  # one bad URL must not kill the batch
            print(f"✗ Unhandled error: {exc}")
            results[url] = f"error: {exc}"
    print("\n===== Batch summary =====")
    for url, outcome in results.items():
        print(f"  {outcome:<20} {url}")
    return 0


def show_pending(cfg: dict) -> int:
    pending = ROOT / cfg["paths"]["pending_dir"]
    cases = sorted(pending.glob("*/case.json")) if pending.exists() else []
    if not cases:
        print("No pending cases. 🎉")
        return 0
    for case_file in cases:
        data = json.loads(case_file.read_text())
        print(f"\n📁 {case_file.parent.name}")
        print(f"   {data.get('title','?')} @ {data.get('company','?')}")
        print(f"   Reason: {data.get('reason','?')}")
        print(f"   URL: {data.get('url','?')}")
        for q in data.get("held_questions", []):
            print(f"   ❓ {q['question']}")
            print(f"      draft: {q['draft_answer'][:150]}")
    print(f"\n{len(cases)} case(s). Resolve one, then delete its folder in pending/.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="job-autopilot")
    parser.add_argument("url", nargs="?", help="Job posting URL")
    parser.add_argument("--dry-run", action="store_true",
                        help="Generate documents only; skip browser automation")
    parser.add_argument("--status", action="store_true", help="Show tracking summary")
    parser.add_argument("--export", metavar="CSV", help="Export tracking log to CSV")
    parser.add_argument("--batch", metavar="FILE", help="File with one job URL per line")
    parser.add_argument("--pending", action="store_true", help="List escalated cases awaiting you")
    parser.add_argument("--answers", metavar="FILE",
                        help="JSON file of approved answers ({question: answer}) from a "
                             "pending case — used to resume a held application")
    args = parser.parse_args()

    cfg = load_config()
    if args.status:
        print(json.dumps(Tracker(ROOT / cfg["paths"]["db_path"]).summary(), indent=2))
        return 0
    if args.pending:
        return show_pending(cfg)
    if args.batch:
        return run_batch(args.batch, args.dry_run)
    if args.export:
        path = Tracker(ROOT / cfg["paths"]["db_path"]).export_csv(args.export)
        print(f"Exported → {path}")
        return 0
    if not args.url:
        parser.print_help()
        return 1
    return run(args.url, args.dry_run, answers_file=args.answers)


if __name__ == "__main__":
    sys.exit(main())
