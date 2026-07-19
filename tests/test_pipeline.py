"""Tests for everything that runs without an API key or browser.

Run:  python -m pytest tests/ -q
"""
import json
import re
import sqlite3
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

from escalation.queue import escalate
from intake.fetcher import Posting, check_exclusions, detect_ats
from tailoring.render import cover_letter_html, render_pdf, resume_html
from tracking.log import Tracker

ROOT = Path(__file__).parent.parent
PROFILE = yaml.safe_load((ROOT / "profile.yaml").read_text())


# ---------- ATS detection ----------
def test_detect_known_ats():
    cases = {
        "https://boards.greenhouse.io/acme/jobs/123": "greenhouse",
        "https://job-boards.greenhouse.io/acme/jobs/123": "greenhouse",
        "https://jobs.lever.co/acme/uuid": "lever",
        "https://acme.wd5.myworkdayjobs.com/en-US/careers/job/x": "workday",
        "https://jobs.ashbyhq.com/acme/uuid": "ashby",
        "https://www.linkedin.com/jobs/view/123": "linkedin",
        "https://careers.example.com/some-job": "unknown",
    }
    for url, expected in cases.items():
        assert detect_ats(url, "") == expected, url


def test_detect_embedded_greenhouse():
    html = '<html><script src="https://boards.greenhouse.io/embed/job_board/js"></script></html>'
    assert detect_ats("https://careers.example.com/jobs/1", html) == "greenhouse"


# ---------- exclusion rules ----------
def _posting(**kw):
    base = dict(url="u", final_url="u", ats="greenhouse", html="",
                title="Software Engineer", company="Acme", description="Python role")
    base.update(kw)
    return Posting(**base)


def test_exclusions_blocklist():
    reason = check_exclusions(_posting(company="El Paso Water Utilities"),
                              {"companies_blocklist": ["El Paso Water"]})
    assert reason and "blocklist" in reason


def test_exclusions_keywords():
    reason = check_exclusions(_posting(description="This role is onsite only in NYC"),
                              {"keywords_reject": ["onsite only"]})
    assert reason and "onsite only" in reason


def test_exclusions_pass():
    assert check_exclusions(_posting(), PROFILE["exclusion_rules"]) is None


# ---------- deterministic validation (no API needed) ----------
def test_validation_catches_fake_bullet_id_and_numbers():
    from tailoring.resume import validate_tailored

    fake = {"jobs": [{"company": "X", "title": "Y",
                      "bullets": [{"id": "not_a_real_id", "text": "did stuff"}]}]}
    problems = validate_tailored({}, PROFILE, fake)
    assert any("does not exist" in p for p in problems)

    inflated = {"jobs": [{"company": "El Paso Water Utilities", "title": "Dev",
                          "bullets": [{"id": "epw_sql",
                                       "text": "Cut retrieval by 99% across 500 systems"}]}]}
    problems = validate_tailored({}, PROFILE, inflated)
    assert any("numbers not in source" in p for p in problems)


def test_validation_passes_faithful_bullets():
    from tailoring.resume import validate_tailored
    # Reuse exact source text — deterministic checks must pass without an API call?
    # (Full pass would call Claude; here we only verify no deterministic problems
    #  by checking the pre-API portion via monkeypatching _ask_json.)
    import tailoring.resume as tr
    original = tr._ask_json
    tr._ask_json = lambda *a, **k: {"problems": []}
    try:
        src = PROFILE["work_history"][0]["bullets"][0]
        faithful = {"jobs": [{"company": "El Paso Water Utilities", "title": "Dev",
                              "bullets": [{"id": src["id"], "text": src["text"]}]}]}
        assert tr.validate_tailored({}, PROFILE, faithful) == []
    finally:
        tr._ask_json = original


# ---------- tracker ----------
def test_tracker_roundtrip(tmp_path):
    t = Tracker(tmp_path / "apps.db")
    app_id = t.record(company="Acme", title="SWE", url="http://x", final_url="http://x",
                      ats="greenhouse", track="swe", status="submitted", reason="ok")
    assert t.duplicate("Acme", "SWE", "http://other") is not None
    assert t.duplicate("Other Co", "SWE", "http://y") is None
    t.update_status(app_id, "failed", "oops")
    assert t.summary() == {"failed": 1}
    csv_path = t.export_csv(tmp_path / "out.csv")
    assert "Acme" in csv_path.read_text()


def test_tracker_dry_runs_and_held_never_block(tmp_path):
    """Only submitted applications count as duplicates — dry runs and held
    (never-submitted) attempts must always be re-runnable."""
    t = Tracker(tmp_path / "apps.db")
    t.record(company="Acme", title="SWE", url="http://x", final_url="http://x",
             ats="greenhouse", status="dry_run", reason="documents generated")
    t.record(company="Acme", title="SWE", url="http://x", final_url="http://x",
             ats="greenhouse", status="held", reason="documents generated")
    assert t.duplicate("Acme", "SWE", "http://x") is None


def test_tracker_duplicate_matches_final_url(tmp_path):
    """A repeat visit via a different entry URL that redirects to the same
    final_url must be caught (regression: only the url column was checked)."""
    t = Tracker(tmp_path / "apps.db")
    t.record(company="", title="", url="http://short.link/abc",
             final_url="http://boards.example/acme/jobs/1",
             ats="greenhouse", status="submitted", reason="ok")
    assert t.duplicate("", "", "http://boards.example/acme/jobs/1") is not None


# ---------- rendering ----------
def test_resume_renders(tmp_path):
    tailored = {
        "summary": PROFILE["tracks"]["swe"]["summary"],
        "skills": PROFILE["tracks"]["swe"]["skills"],
        "jobs": [{"company": j["company"], "title": j["title"],
                  "bullets": [{"id": b["id"], "text": b["text"]} for b in j["bullets"]]}
                 for j in PROFILE["work_history"]],
        "project_keys_ordered": PROFILE["tracks"]["swe"]["project_order"],
    }
    html = resume_html(PROFILE, tailored, "swe")
    assert "Tyler Salas" in html and "El Paso Water" in html and "95%" in html
    out = render_pdf(html, tmp_path / "resume.pdf")
    assert out.exists() and out.stat().st_size > 1000


def test_cover_letter_renders(tmp_path):
    html = cover_letter_html(PROFILE, "Para one.\n\nPara two.\n\nPara three.")
    assert html.count("Dear Hiring Manager,") == 1
    assert html.count("Sincerely,") == 1
    assert html.count("Tyler Salas") == 2  # header + sign-off, nowhere else
    out = render_pdf(html, tmp_path / "cl.pdf")
    assert out.exists()


def test_third_person_detection():
    from tailoring.resume import sounds_third_person
    assert sounds_third_person(PROFILE, "Two pieces of Tyler's background speak to this role.")
    assert sounds_third_person(PROFILE, "At El Paso Water Utilities, he built Python pipelines.")
    assert sounds_third_person(PROFILE, "His projects extend that pattern.")
    assert not sounds_third_person(
        PROFILE, "I built Python pipelines at El Paso Water Utilities. "
                 "My projects extend that pattern, and I would welcome a conversation.")


def test_strip_greeting_signoff():
    from tailoring.resume import strip_greeting_signoff
    text = ("Dear Hiring Manager,\n\nI am applying for the role. Body continues "
            "with my regards to the team.\n\nClosing sentence here.\n\nSincerely,\nTyler Salas")
    cleaned = strip_greeting_signoff(text)
    assert "Dear Hiring Manager" not in cleaned
    assert "Sincerely" not in cleaned and "Tyler Salas" not in cleaned
    assert cleaned.startswith("I am applying")
    assert cleaned.endswith("Closing sentence here.")
    # mid-letter 'regards' must survive; text without greeting/sign-off unchanged
    assert "my regards to the team" in cleaned
    assert strip_greeting_signoff("Plain body only.") == "Plain body only."


# ---------- escalation ----------
def test_escalation_case_created(tmp_path):
    case = escalate(tmp_path, reason="CAPTCHA", url="http://x", company="Acme Inc.",
                    title="SWE", page_html="<html/>", extra={"note": "hi"})
    data = json.loads((case / "case.json").read_text())
    assert data["reason"] == "CAPTCHA" and data["note"] == "hi"
    assert (case / "page.html").exists()


# ---------- skills sanitizer (anti-fabrication, deterministic) ----------
def test_sanitize_skills_drops_invented_and_maps_embellished():
    from tailoring.resume import sanitize_skills
    source = {"Languages": ["Python", "Java", "SQL"],
              "Frameworks & Libraries": ["FastAPI", "Angular", "Pytest"]}
    tailored = {"skills": {
        "Languages": ["Python", "SQL", "Rust"],                    # Rust invented
        "Testing": ["Pytest (unit + integration)"],                # embellished → Pytest
        "AI-Native Development": ["AI coding tools (Claude)"],     # fully invented section
    }}
    sanitize_skills(tailored, source)
    assert tailored["skills"] == {"Languages": ["Python", "SQL"],
                                  "Testing": ["Pytest"]}


def test_sanitize_skills_falls_back_to_source_when_all_invented():
    from tailoring.resume import sanitize_skills
    source = {"Languages": ["Python"]}
    tailored = {"skills": {"Made Up": ["Quantum Basket Weaving"]}}
    sanitize_skills(tailored, source)
    assert tailored["skills"] == {"Languages": ["Python"]}


def test_company_from_greenhouse_url():
    from intake.fetcher import company_from_greenhouse_url
    assert company_from_greenhouse_url(
        "https://job-boards.greenhouse.io/airtable/jobs/8400373002") == "Airtable"
    assert company_from_greenhouse_url(
        "https://boards.greenhouse.io/el-paso-water/jobs/1") == "El Paso Water"
    assert company_from_greenhouse_url("https://example.com/careers") == ""


def test_detect_embedded_greenhouse_via_gh_jid():
    html = '<html><a href="https://acme.com/careers?gh_jid=8560779002">Apply</a></html>'
    assert detect_ats("https://acme.com/careers/swe", html) == "greenhouse"


# ---------- choice questions (selects / radios), no API needed ----------
def _handler():
    from handlers.base import BaseHandler
    cfg = {"automation": {"human_delay_ms": [0, 0]}, "paths": {"pending_dir": "pending"}}
    return BaseHandler(cfg, PROFILE, _posting(), {"jd_text": ""}, None)


def test_choice_value_maps_standard_answers():
    h = _handler()
    assert h.choice_value("Will you now or in the future require sponsorship?") == "No"
    assert h.choice_value("Are you legally authorized to work in the United States?") == "Yes"
    assert h.choice_value("Veteran Status") == "I am not a protected veteran"
    assert h.choice_value("Do you have a disability?") == "No, I do not have a disability"
    assert h.choice_value("Gender") == "Male"
    assert h.choice_value("Are you Hispanic or Latino?") == "Yes"
    assert h.choice_value("Are you willing to relocate?") == "Yes"
    assert h.choice_value("Are you at least 18 years of age?") == "Yes"
    assert h.choice_value("How did you hear about this job?") == "Company careers page"
    assert h.choice_value("Have you ever been convicted of a felony?") == "No"
    assert h.choice_value("Country*") == "United States"
    assert h.choice_value("Describe a project you're proud of") is None  # not a choice we know


def test_choice_value_noncompete_and_sms():
    h = _handler()
    assert h.choice_value(
        "Are you subject to an agreement with a former employer or other party "
        "(such as non-competition or non-solicitation agreement) that might, in any way, "
        "restrict your ability to work for our Company?") == "No"
    assert h.choice_value(
        "Would you like to opt-in to receiving text messages for this role regarding "
        "the hiring process (e.g., interview requests and reminders)?") == "Yes"
    assert h.choice_value(
        "Do you currently require the company's sponsorship or need the company's "
        "assistance to obtain or maintain authorization to work legally in the United "
        "States? This includes, but is not limited to, H-1B visas (lottery and "
        "transfers), TN visas, E-3 visas, or other company-sponsored visas.") == "No"


def test_match_option_verbose_sms_labels():
    h = _handler()
    opts = ["Yes, I do want to opt-in to receiving text messages as stated above.",
            "No, I do not want to opt-in to receiving text messages as stated above."]
    assert h.match_option("Yes", opts) == 0
    assert h.match_option("No", opts) == 1


def test_company_from_path_slug():
    from intake.fetcher import company_from_path_slug
    assert company_from_path_slug(
        "https://jobs.lever.co/supermove/176fe130-a2cc-40da-b978-8c693fefc510") == "Supermove"
    assert company_from_path_slug(
        "https://jobs.ashbyhq.com/g2i/3d86ecbd-76e5-491b-b271-4c88aeeb7746/application") == "G2I"
    assert company_from_path_slug("https://jobs.lever.co/") == ""


def test_standard_value_twitter_and_portfolio():
    h = _handler()
    assert h.standard_value("Twitter URL") == "None"
    assert h.standard_value("X Profile") == "None"
    github = PROFILE["identity"]["github"]
    assert h.standard_value("Portfolio URL") == github
    assert h.standard_value("Personal website") == github
    assert h.standard_value("GitHub URL") == github


def test_match_option_yes_no_and_eeo():
    h = _handler()
    assert h.match_option("No", ["Yes", "No", "Decline to self identify"]) == 1
    assert h.match_option("Yes", ["Yes, I am authorized", "No, I am not"]) == 0
    # bare yes/no must never substring-match into unrelated options
    assert h.match_option("No", ["Unknown", "Not applicable"]) is None
    eeo_vet = ["I identify as one or more of the classifications of a protected veteran",
               "I am not a protected veteran",
               "I don't wish to answer"]
    assert h.match_option("I am not a protected veteran", eeo_vet) == 1
    eeo_dis = ["Yes, I have a disability (or previously had one)",
               "No, I do not have a disability",
               "I do not want to answer"]
    assert h.match_option("No, I do not have a disability", eeo_dis) == 1
    assert h.match_option("Hispanic or Latino",
                          ["White", "Hispanic or Latino", "Two or More Races"]) == 1
    assert h.match_option("US citizen", ["U.S. Citizen", "Permanent Resident", "Other"]) == 0
    assert h.match_option("nonexistent", ["A", "B"]) is None


def test_answer_choice_prefers_profile_over_api():
    """Deterministic path must not touch the API (draft_choice_answer)."""
    import tailoring.resume as tr
    h = _handler()
    original = tr.draft_choice_answer
    tr.draft_choice_answer = lambda *a, **k: (_ for _ in ()).throw(AssertionError("API called"))
    try:
        res = h.answer_choice("Do you require sponsorship?", ["Yes", "No"])
        assert res == {"value": "No", "hold": False, "source": "profile"}
    finally:
        tr.draft_choice_answer = original


def test_answer_choice_holds_on_low_confidence():
    import tailoring.resume as tr
    h = _handler()
    original = tr.draft_choice_answer
    tr.draft_choice_answer = lambda *a, **k: {"choice": "Blue", "confidence": "low"}
    try:
        res = h.answer_choice("Favorite color?", ["Blue", "Red"])
        assert res["hold"] is True
    finally:
        tr.draft_choice_answer = original


def test_review_mode_falls_back_to_hold_without_terminal():
    """essay_policy: review must escalate (hold), not hang, when stdin isn't a
    TTY — e.g. phone-driven runs through Claude Code."""
    import tailoring.resume as tr
    from handlers.base import BaseHandler
    cfg = {"automation": {"human_delay_ms": [0, 0], "essay_policy": "review"},
           "paths": {"pending_dir": "pending"}}
    h = BaseHandler(cfg, PROFILE, _posting(), {"jd_text": ""}, None)
    original = tr.draft_field_answer
    tr.draft_field_answer = lambda *a, **k: {"answer": "essay draft",
                                             "confidence": "high", "is_essay": True}
    try:
        res = h.answer_custom_question("Describe your ideal role")
        assert res["hold"] is True          # pytest stdin is not a TTY
    finally:
        tr.draft_field_answer = original


def test_review_accepts_draft_on_enter(monkeypatch):
    import sys as _sys
    from handlers.base import BaseHandler
    cfg = {"automation": {"human_delay_ms": [0, 0], "essay_policy": "review"},
           "paths": {"pending_dir": "pending"}}
    h = BaseHandler(cfg, PROFILE, _posting(), {"jd_text": ""}, None)
    monkeypatch.setattr(_sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *a: "")
    assert h.review_answer("Q?", "the draft") == "the draft"
    monkeypatch.setattr("builtins.input", lambda *a: "2")
    assert h.review_choice("Q?", ["A", "B", "C"]) == "B"
    monkeypatch.setattr("builtins.input", lambda *a: "s")
    assert h.review_answer("Q?", "the draft") is None
    assert h.review_choice("Q?", ["A", "B"]) is None


def test_review_redraft_option(monkeypatch):
    """[r] fetches a new Claude draft; Enter then accepts it."""
    import sys as _sys
    from handlers.base import BaseHandler
    cfg = {"automation": {"human_delay_ms": [0, 0], "essay_policy": "review"},
           "paths": {"pending_dir": "pending"}}
    h = BaseHandler(cfg, PROFILE, _posting(), {"jd_text": ""}, None)
    monkeypatch.setattr(_sys.stdin, "isatty", lambda: True)
    inputs = iter(["r", ""])
    monkeypatch.setattr("builtins.input", lambda *a: next(inputs))
    result = h.review_answer("Q?", "old draft", redraft=lambda prev: f"new draft (was: {prev})")
    assert result == "new draft (was: old draft)"


# ---------- approve-and-resume loop ----------
def test_escalation_writes_approved_answers_template(tmp_path):
    case = escalate(tmp_path, reason="held", url="http://x", company="Acme",
                    title="SWE", extra={"held_questions": [
                        {"question": "Why us?", "draft_answer": "Because reasons."}]})
    template = json.loads((case / "approved_answers.json").read_text())
    assert template == {"Why us?": "Because reasons."}


def test_identity_value_ignores_question_length_labels():
    """Long labels are questions, not identity fields — 'team specific
    locations' inside a question must not pattern-match as 'location'."""
    h = _handler()
    assert h.identity_value("Location") == "El Paso, TX"
    q = ("If you are not based out of the SFBA/NYC/Seattle, please answer: "
         "1) Where are you currently located? 2) Would you be willing to "
         "relocate? (see job description for team specific locations)")
    assert h.identity_value(q) is None


def test_relocation_question_gets_standing_answer():
    """Free-text relocation questions use the profile's relocation_answer
    deterministically — no API call, never held."""
    import tailoring.resume as tr
    from handlers.base import BaseHandler
    cfg = {"automation": {"human_delay_ms": [0, 0], "essay_policy": "hold"},
           "paths": {"pending_dir": "pending"}}
    h = BaseHandler(cfg, PROFILE, _posting(), {"jd_text": ""}, None)
    original = tr.draft_field_answer
    tr.draft_field_answer = lambda *a, **k: (_ for _ in ()).throw(AssertionError("API called"))
    try:
        res = h.answer_custom_question(
            "If you are not based out of the SFBA/NYC/Seattle, please answer: "
            "1) Where are you currently located? 2) Would you be willing to relocate?")
        assert res["hold"] is False
        assert "willing to relocate" in res["answer"]
    finally:
        tr.draft_field_answer = original


def test_approved_answers_bypass_hold():
    from handlers.base import BaseHandler
    cfg = {"automation": {"human_delay_ms": [0, 0], "essay_policy": "hold"},
           "paths": {"pending_dir": "pending"}}
    h = BaseHandler(cfg, PROFILE, _posting(),
                    {"jd_text": "", "approved_answers": {
                        "Why us?*": "My edited answer."}}, None)
    # matches despite the trailing asterisk (normalized containment)
    res = h.answer_custom_question("Why us?")
    assert res == {"answer": "My edited answer.", "confidence": "high",
                   "is_essay": False, "hold": False, "source": "approved"}


# ---------- Phase 3 additions ----------
def test_registry_maps_all_supported_ats():
    from handlers.registry import REGISTRY, get_handler
    from intake.fetcher import SUPPORTED_ATS
    assert SUPPORTED_ATS == set(REGISTRY)          # never claim support without a handler
    assert get_handler("greenhouse").ats_name == "greenhouse"
    assert get_handler("lever").ats_name == "lever"
    assert get_handler("ashby").ats_name == "ashby"
    assert get_handler("workday").ats_name == "workday"
    assert get_handler("icims") is None


def test_batch_file_parsing(tmp_path):
    import main as m
    batch = tmp_path / "urls.txt"
    batch.write_text("# comment\nhttps://a.example/1\n\nhttps://b.example/2\n")
    calls = []
    original = m.run
    m.run = lambda url, dry: calls.append(url) or 0
    try:
        rc = m.run_batch(str(batch), dry_run=True)
    finally:
        m.run = original
    assert rc == 0 and calls == ["https://a.example/1", "https://b.example/2"]


def test_batch_isolates_failures(tmp_path):
    import main as m
    batch = tmp_path / "urls.txt"
    batch.write_text("https://bad.example/x\nhttps://good.example/y\n")
    calls = []
    def flaky(url, dry):
        calls.append(url)
        if "bad" in url:
            raise RuntimeError("boom")
        return 0
    original = m.run
    m.run = flaky
    try:
        rc = m.run_batch(str(batch), dry_run=True)
    finally:
        m.run = original
    assert rc == 0 and len(calls) == 2   # second URL still ran


# ---------- API retry/backoff ----------
def _fake_api_error(cls, status, headers=None):
    import httpx
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    resp = httpx.Response(status, request=req, headers=headers or {})
    return cls("simulated", response=resp, body=None)


class _FlakyMessages:
    """messages.create that raises the queued errors, then succeeds."""
    def __init__(self, errors):
        self.errors = list(errors)
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return "ok"


def _run_create(monkeypatch, errors, max_retries=5):
    import anthropic
    import tailoring.resume as tr
    flaky = _FlakyMessages(errors)
    fake_client = type("C", (), {"messages": flaky})()
    monkeypatch.setattr(tr, "client", lambda: fake_client)
    sleeps = []
    monkeypatch.setattr(tr.time, "sleep", lambda s: sleeps.append(s))
    cfg = {"anthropic": {"max_retries": max_retries}}
    return tr._create(cfg, model="m", max_tokens=10, messages=[]), flaky, sleeps


def test_create_retries_transient_errors_then_succeeds(monkeypatch):
    import anthropic
    errors = [
        _fake_api_error(anthropic.RateLimitError, 429),
        _fake_api_error(anthropic.InternalServerError, 529),
    ]
    result, flaky, sleeps = _run_create(monkeypatch, errors)
    assert result == "ok" and flaky.calls == 3 and len(sleeps) == 2
    assert sleeps[1] > 0  # backed off, didn't hammer


def test_create_honors_retry_after_header(monkeypatch):
    import anthropic
    errors = [_fake_api_error(anthropic.RateLimitError, 429,
                              headers={"retry-after": "7"})]
    result, _, sleeps = _run_create(monkeypatch, errors)
    assert result == "ok" and sleeps == [7.0]


def test_create_gives_up_after_max_retries(monkeypatch):
    import anthropic
    import pytest
    errors = [_fake_api_error(anthropic.InternalServerError, 500)
              for _ in range(3)]
    with pytest.raises(anthropic.InternalServerError):
        _run_create(monkeypatch, errors, max_retries=2)


def test_create_does_not_retry_nonretryable(monkeypatch):
    import anthropic
    import pytest
    import tailoring.resume as tr
    errors = [_fake_api_error(anthropic.AuthenticationError, 401)]
    flaky = _FlakyMessages(errors)
    fake_client = type("C", (), {"messages": flaky})()
    monkeypatch.setattr(tr, "client", lambda: fake_client)
    monkeypatch.setattr(tr.time, "sleep", lambda s: (_ for _ in ()).throw(
        AssertionError("must not sleep on non-retryable errors")))
    with pytest.raises(anthropic.AuthenticationError):
        tr._create({"anthropic": {"max_retries": 5}}, model="m",
                   max_tokens=10, messages=[])
    assert flaky.calls == 1


# ---------- pronoun checkbox groups (Lever pronouns widget) ----------
def test_pronoun_question_matches_option_case_insensitively():
    """Claude drafts 'He/Him'; Lever's option is 'He/him' — must match."""
    h = _handler()
    options = ["He/him", "She/her", "They/them", "Xe/xem", "Use name only"]
    assert h.match_option("He/Him", options) == 0


def test_pronoun_choice_uses_profile_when_present():
    """A standard_answers.pronouns entry answers pronoun questions
    deterministically — no API call."""
    import copy
    import tailoring.resume as tr
    from handlers.base import BaseHandler
    profile = copy.deepcopy(PROFILE)
    profile["standard_answers"]["pronouns"] = "He/him"
    cfg = {"automation": {"human_delay_ms": [0, 0], "essay_policy": "hold"},
           "paths": {"pending_dir": "pending"}}
    h = BaseHandler(cfg, profile, _posting(), {"jd_text": ""}, None)
    original = tr.draft_choice_answer
    tr.draft_choice_answer = lambda *a, **k: (_ for _ in ()).throw(AssertionError("API called"))
    try:
        res = h.answer_choice("Pronouns", ["He/him", "She/her", "They/them"])
        assert res == {"value": "He/him", "hold": False, "source": "profile"}
    finally:
        tr.draft_choice_answer = original


def test_pronoun_choice_without_profile_falls_back_to_claude():
    import copy
    import tailoring.resume as tr
    from handlers.base import BaseHandler
    profile = copy.deepcopy(PROFILE)
    profile["standard_answers"].pop("pronouns", None)  # simulate absence
    cfg = {"automation": {"human_delay_ms": [0, 0], "essay_policy": "hold"},
           "paths": {"pending_dir": "pending"}}
    h = BaseHandler(cfg, profile, _posting(), {"jd_text": ""}, None)
    assert h.choice_value("Pronouns") is None  # not in profile → Claude's turn
    original = tr.draft_choice_answer
    tr.draft_choice_answer = lambda *a, **k: {"choice": "He/Him", "confidence": "high"}
    try:
        res = h.answer_choice("Pronouns", ["He/him", "She/her", "They/them"])
        assert res["value"] == "He/him" and res["hold"] is False
    finally:
        tr.draft_choice_answer = original


# ---------- education year fields vs. start-date mapping ----------
def test_education_year_labels_do_not_get_start_date_answer():
    """'Start date year' (education section, input[type=number]) must not
    receive the free-text earliest-start-date sentence."""
    h = _handler()
    assert h.standard_value("Start date year") is None
    assert h.standard_value("Start date month") is None


def test_real_start_date_questions_still_answered():
    h = _handler()
    assert h.standard_value("Earliest start date") == "2 weeks from offer acceptance"
    assert h.standard_value("When are you available to start?") \
        == "2 weeks from offer acceptance"


# ---------- greenhouse redirects: closed postings & embedded boards ----------
class _FakeResp:
    def __init__(self, url, text):
        self.url, self.text = url, text
    def raise_for_status(self):
        pass


def test_closed_greenhouse_posting_detected(monkeypatch):
    """Dead posting → redirect to board index with ?error=true → closed."""
    import intake.fetcher as f
    monkeypatch.setattr(f.requests, "get", lambda *a, **k: _FakeResp(
        "https://job-boards.greenhouse.io/affirm?error=true",
        "<html><h1>Jobs at Affirm</h1></html>"))
    p = f.fetch_posting("https://job-boards.greenhouse.io/affirm/jobs/123")
    assert p.closed is True


def test_embedded_greenhouse_redirect_rewrites_to_embed_form(monkeypatch):
    """boards URL redirecting to the company site (JS-embedded form) must
    point the handler at the direct embed job_app URL — and NOT be
    mistaken for a closed posting."""
    import intake.fetcher as f
    monkeypatch.setattr(f.requests, "get", lambda *a, **k: _FakeResp(
        "https://www.samsara.com/company/careers/roles/7619925?gh_jid=7619925",
        "<html><div id='grnhse_app'></div><h1>Software Engineer II</h1></html>"))
    p = f.fetch_posting("https://boards.greenhouse.io/samsara/jobs/7619925")
    assert p.closed is False
    assert p.final_url == ("https://job-boards.greenhouse.io/embed/job_app"
                           "?for=samsara&token=7619925")


def test_normal_greenhouse_posting_unaffected(monkeypatch):
    import intake.fetcher as f
    monkeypatch.setattr(f.requests, "get", lambda *a, **k: _FakeResp(
        "https://job-boards.greenhouse.io/smartsheet/jobs/7712828",
        "<html><h1>Software Engineer II</h1></html>"))
    p = f.fetch_posting("https://job-boards.greenhouse.io/smartsheet/jobs/7712828")
    assert p.closed is False
    assert p.final_url == "https://job-boards.greenhouse.io/smartsheet/jobs/7712828"


# ---------- education auto-fill ----------
def test_degree_candidates_for_bs():
    from handlers.greenhouse import GreenhouseHandler
    cands = GreenhouseHandler.degree_candidates("B.S. in Computer Science")
    assert cands[0] == "Bachelor of Science"
    assert "Bachelor's Degree" in cands


def test_degree_matching_against_common_greenhouse_lists():
    h = _handler()
    from handlers.greenhouse import GreenhouseHandler
    cands = GreenhouseHandler.degree_candidates("B.S. in Computer Science")
    for options in (
        ["Bachelor of Arts", "Bachelor of Science", "Master of Science"],
        ["High School", "Associate's Degree", "Bachelor's Degree", "Master's Degree"],
    ):
        assert any(h.match_option(c, options) is not None for c in cands), options


def test_discipline_extracted_from_degree_string():
    import re as _re
    assert _re.sub(r"^.*?\bin\b\s+", "", "B.S. in Computer Science").strip() \
        == "Computer Science"


# ---------- fuzzy matching for school & location pickers ----------
def test_fuzzy_matches_reformatted_school_name():
    h = _handler()
    options = ["University Houston - Downtown", "University Texas - Arlington",
               "University Texas - El Paso", "University Texas - San Antonio"]
    assert h.fuzzy_index("The University of Texas at El Paso", options) == 2


def test_fuzzy_matches_location_suggestion():
    h = _handler()
    options = ["El Paso, Texas, United States", "El Paso, Illinois, United States"]
    # Both options contain the city tokens — the first (most relevant)
    # suggestion wins.
    assert h.fuzzy_index("El Paso", options) == 0


def test_fuzzy_never_matches_short_answers():
    """Yes/No-style answers must never fuzzy-match (single significant
    token) — fuzzy is for proper nouns only."""
    h = _handler()
    assert h.fuzzy_index("Yes", ["Yes, with assistance", "No"]) is None


# ---------- standing answers: transgender & how-did-you-hear ----------
def test_transgender_question_not_answered_with_gender():
    """'I identify as transgender:' contains 'gender' — must resolve via the
    transgender standing answer, never the gender value."""
    h = _handler()
    assert h.choice_value("I identify as transgender:") == "No"
    assert h.choice_value("I identify my gender as:") == "Male"


def test_how_did_you_hear_free_text_standing_answer():
    import tailoring.resume as tr
    h = _handler()
    original = tr.draft_field_answer
    tr.draft_field_answer = lambda *a, **k: (_ for _ in ()).throw(AssertionError("API called"))
    try:
        res = h.answer_custom_question("How did you hear about this position?")
        assert res["hold"] is False and res["answer"] == "Company careers page"
    finally:
        tr.draft_field_answer = original


def test_hear_about_choice_matches_branded_careers_option():
    """'Smartsheet Careers Site' doesn't text-match 'Company careers page' —
    the careers fallback must still resolve it deterministically (no API,
    no in-terminal prompt)."""
    import tailoring.resume as tr
    h = _handler()
    original = tr.draft_choice_answer
    tr.draft_choice_answer = lambda *a, **k: (_ for _ in ()).throw(AssertionError("API called"))
    try:
        res = h.answer_choice("How did you hear about us?",
                              ["LinkedIn", "Employee Referral",
                               "Smartsheet Careers Site", "Job Board", "Other"])
        assert res == {"value": "Smartsheet Careers Site", "hold": False,
                       "source": "profile"}
    finally:
        tr.draft_choice_answer = original


def test_hear_about_choice_prefers_careers_over_career_fair():
    h = _handler()
    assert h.careers_option_index(["Career Fair", "Careers Page"]) == 1
    assert h.careers_option_index(["Career Fair", "Company Website"]) == 1


def test_hear_about_matches_jobs_site_variant():
    """Red Hat's Workday source list says 'Red Hat Jobs Site' — a jobs-site
    option is the company careers site (live 2026-07-18). 'Job Board' must
    stay excluded (no page/site tail)."""
    h = _handler()
    assert h.careers_option_index(["Red Hat Jobs Site"]) == 0
    assert h.careers_option_index(["LinkedIn", "Job Board", "Other"]) == 2


def test_hear_about_choice_falls_back_to_other():
    """No careers-like option at all → 'Other', per user 2026-07-14."""
    import tailoring.resume as tr
    h = _handler()
    original = tr.draft_choice_answer
    tr.draft_choice_answer = lambda *a, **k: (_ for _ in ()).throw(AssertionError("API called"))
    try:
        res = h.answer_choice("How did you hear about this job?",
                              ["LinkedIn", "Job Board", "Referral", "Other"])
        assert res == {"value": "Other", "hold": False, "source": "profile"}
    finally:
        tr.draft_choice_answer = original


def test_careers_fallback_only_for_hear_about_questions():
    """The careers fallback must never leak into unrelated choice
    questions that happen to have a careers-ish option."""
    h = _handler()
    idx = h.match_option("Male", ["Careers Page", "Male", "Female"])
    assert idx == 1  # normal matching unaffected
    assert h.careers_option_index(["Yes", "No"]) is None


def test_typeahead_queries_add_distinctive_tail():
    """School searches try each candidate verbatim, then the last two
    significant tokens ('el paso') so the backend finds the entry no matter
    how it formats the name."""
    from handlers.greenhouse import GreenhouseHandler
    qs = GreenhouseHandler._typeahead_queries(
        ["The University of Texas at El Paso", "University of Texas - El Paso"])
    assert qs[0] == "The University of Texas at El Paso"
    assert qs[1] == "University of Texas - El Paso"
    assert qs[2] == "el paso"
    assert len(qs) == 3  # tail deduped across candidates


def test_handlers_never_call_locator_only_methods_on_element_handles():
    """Handlers work with ElementHandles (query_selector), not Locators.
    Locator-only methods (press_sequentially, and_, or_...) raise
    AttributeError at runtime — which the handlers' broad except blocks
    swallow, silently skipping the field (this killed location AND school
    fills on 2026-07-14). Guard at the source level."""
    locator_only = ["press_sequentially", "wait_for(", ".and_(", ".or_("]
    for path in (ROOT / "handlers").glob("*.py"):
        src = path.read_text()
        for name in locator_only:
            assert name not in src, f"{path.name} uses Locator-only API {name}"


def test_school_aliases_read_from_profile():
    edu = PROFILE["education"][0]
    cands = [edu.get("school")] + list(edu.get("school_aliases") or [])
    assert cands == ["The University of Texas at El Paso",
                     "University of Texas - El Paso"]


# ---------- Ashby react widgets (mock-DOM) ----------
# Fakes mirror the DOM captured live 2026-07-14 (see handlers/ashby.py
# docstring): comboboxes commit the chosen text into input.value; options
# live in a body-portaled [role='listbox']; Boolean questions are Yes/No
# button pairs whose chosen button gains an `_active` class.

class _FakeKeyboard:
    def press(self, key):
        pass


class _FakeOption:
    def __init__(self, text, on_click=None, is_pill=False):
        self._text, self._on_click = text, on_click
        self._is_pill = is_pill  # selected pills reuse promptOption (Workday)

    def is_visible(self):
        return True

    def inner_text(self):
        return self._text

    def evaluate(self, js):
        if "selectedItemList" in js:
            return self._is_pill
        return False

    def click(self, **kw):
        if self._on_click:
            self._on_click(self._text)


class _FakeCombo:
    """input[role='combobox']: static lists open on click; async typeaheads
    surface options only after type()."""

    def __init__(self, page, label, static_options=None, search=None, required=True):
        self._page, self.label, self.required = page, label, required
        self.value = ""
        self._static = static_options or []
        self._search = search or {}  # typed query -> option texts
        self.typed = []

    def is_visible(self):
        return True

    def input_value(self):
        return self.value

    def click(self):
        self._page.menu = [_FakeOption(t, self._commit) for t in self._static]

    def fill(self, v):
        self.value = v or ""

    def type(self, text, delay=0):
        self.typed.append(text)
        self._page.menu = [_FakeOption(t, self._commit)
                           for t in self._search.get(text, [])]

    def _commit(self, text):
        self.value = text
        self._page.menu = []

    def get_attribute(self, name):
        return {"role": "combobox"}.get(name)

    def evaluate(self, js):
        return self.required if "_required_" in js else self.label


class _FakeYesNoButton:
    def __init__(self, group, text):
        self._group, self._text = group, text

    def is_visible(self):
        return True

    def inner_text(self):
        return self._text

    def click(self):
        self._group.clicked = self._text


class _FakeYesNoGroup:
    def __init__(self, label, answered=False, required=True):
        self.label, self.answered, self.required = label, answered, required
        self.clicked = None
        self.buttons = [_FakeYesNoButton(self, "Yes"), _FakeYesNoButton(self, "No")]

    def query_selector(self, sel):
        assert "_active" in sel
        return object() if (self.answered or self.clicked) else None

    def query_selector_all(self, sel):
        return self.buttons

    def evaluate(self, js):
        return self.required if "_required_" in js else self.label


class _FakeAshbyPage:
    def __init__(self, combos=(), yesno=()):
        self.combos, self.yesno = list(combos), list(yesno)
        self.menu = []
        self.keyboard = _FakeKeyboard()

    def wait_for_timeout(self, ms):
        pass

    def query_selector(self, sel):
        return None  # only the poll's "No results" listbox probe lands here

    def query_selector_all(self, sel):
        if "combobox" in sel:
            return self.combos
        if "_yesno" in sel:
            return self.yesno
        if "option" in sel:
            return self.menu
        return []


def _ashby_handler():
    from handlers.ashby import AshbyHandler
    cfg = {"automation": {"human_delay_ms": [0, 0]}, "paths": {"pending_dir": "pending"}}
    return AshbyHandler(cfg, PROFILE, _posting(ats="ashby"), {"jd_text": ""}, None)


def test_ashby_location_combobox_typeahead_picks_profile_city():
    h = _ashby_handler()
    page = _FakeAshbyPage()
    combo = _FakeCombo(page, "Where are you currently located?",
                       search={"El Paso": ["El Paso, Texas, United States",
                                           "El Paso, Illinois, United States"]})
    page.combos = [combo]
    held = h.handle_comboboxes(page)
    assert held == []
    assert combo.value == "El Paso, Texas, United States"
    assert combo.typed == ["El Paso"]  # city query first for city-style asks


def test_ashby_country_combobox_tries_country_query_first():
    h = _ashby_handler()
    page = _FakeAshbyPage()
    combo = _FakeCombo(page, "Which country do you intend to work from?",
                       search={"United States": ["United States"]})
    page.combos = [combo]
    held = h.handle_comboboxes(page)
    assert held == []
    assert combo.value == "United States"
    assert combo.typed[0] == "United States"


def test_ashby_static_combobox_resolves_hear_about_without_holding():
    h = _ashby_handler()
    page = _FakeAshbyPage()
    combo = _FakeCombo(page, "How did you hear about Acme?",
                       static_options=["Acme Careers Site", "Twitter", "A friend",
                                       "Other"])
    page.combos = [combo]
    held = h.handle_comboboxes(page)
    assert held == []
    assert combo.value == "Acme Careers Site"


def test_ashby_unknown_async_combobox_is_held_never_guessed():
    h = _ashby_handler()
    page = _FakeAshbyPage()
    combo = _FakeCombo(page, "Which university did you attend?",
                       search={})  # async search we have no query for
    page.combos = [combo]
    held = h.handle_comboboxes(page)
    assert len(held) == 1 and held[0]["question"] == "Which university did you attend?"
    assert combo.value == ""


def test_ashby_filled_combobox_left_alone():
    h = _ashby_handler()
    page = _FakeAshbyPage()
    combo = _FakeCombo(page, "Where are you currently located?")
    combo.value = "Austin, Texas, United States"  # e.g. autofilled from resume
    page.combos = [combo]
    assert h.handle_comboboxes(page) == []
    assert combo.value == "Austin, Texas, United States"
    assert combo.typed == []


def test_ashby_yesno_buttons_answer_from_profile():
    h = _ashby_handler()
    auth = _FakeYesNoGroup(
        "Are you authorized to work in the country where the job is located?")
    sponsor = _FakeYesNoGroup(
        "Will you now or in the future require sponsorship for employment?")
    held = h.handle_yesno_buttons(_FakeAshbyPage(yesno=[auth, sponsor]))
    assert held == []
    assert auth.clicked == "Yes"
    assert sponsor.clicked == "No"


def test_ashby_yesno_answered_group_left_alone():
    h = _ashby_handler()
    grp = _FakeYesNoGroup("Are you authorized to work here?", answered=True)
    assert h.handle_yesno_buttons(_FakeAshbyPage(yesno=[grp])) == []
    assert grp.clicked is None


def test_ethnicity_matches_latine_and_other_variants():
    """Ethnicity questions ALWAYS answer Hispanic/Latino (user 2026-07-14) —
    option wording varies per form and must never fall through to Claude."""
    h = _handler()
    for variant in ("Hispanic or Latine", "Hispanic or Latino", "Latinx",
                    "Hispanic/Latina/o"):
        opts = ["Asian or Asian American", "Black or African American", variant,
                "White", "Other", "I prefer not to answer"]
        res = h.answer_choice(
            "Which ethnicity(ies) do you identify with? Please select all that apply.",
            opts)
        assert res == {"value": variant, "hold": False, "source": "profile"}, variant


def test_ethnicity_never_matches_negated_options():
    """Red Hat labels every non-Hispanic race '... (Not Hispanic or Latino)'
    — the run picked 'American Indian or Alaska Native' (2026-07-18).
    Negated mentions must not count; the real Hispanic option must win
    even though it appears fourth."""
    h = _handler()
    opts = ["American Indian or Alaska Native (Not Hispanic or Latino) "
            "(United States of America)",
            "Asian (Not Hispanic or Latino) (United States of America)",
            "Black or African American (Not Hispanic or Latino) "
            "(United States of America)",
            "Hispanic or Latino (United States of America)",
            "White (Not Hispanic or Latino) (United States of America)",
            "I do not wish to answer (United States of America)"]
    res = h.answer_choice("Please select your race/ethnicity.", opts)
    assert res == {"value": "Hispanic or Latino (United States of America)",
                   "hold": False, "source": "profile"}
    assert h.ethnicity_option_index(opts) == 3


def test_communities_question_answers_none_of_the_above():
    """'Which communities do you belong to?' surveys → 'None of the above'
    (user 2026-07-14)."""
    h = _handler()
    opts = ["Person with disability", "Neurodivergent", "Veteran", "Parent",
            "Refugee or immigrant", "None of the above", "I prefer not to answer"]
    res = h.answer_choice(
        "Which of the following communities do you belong to? Please select all that apply.",
        opts)
    assert res == {"value": "None of the above", "hold": False, "source": "profile"}


def test_age_range_options_resolve_from_profile_age():
    """Tyler is 26 (user 2026-07-14): '25-34'-style ranges must resolve
    deterministically; over-18 questions must keep answering Yes."""
    h = _handler()
    res = h.answer_choice("What is your current age?",
                          ["Under 18", "18-24", "25-34", "35-44", "45+",
                           "I prefer not to answer"])
    assert res == {"value": "25-34", "hold": False, "source": "profile"}
    res = h.answer_choice("How old are you?", ["18 to 24", "25 to 34", "35 or older"])
    assert res["value"] == "25 to 34"
    assert h.choice_value("Are you at least 18 years of age?") == "Yes"
    assert h.age_option_index(26, ["Under 25", "26+"]) == 1
    assert h.age_option_index(17, ["Under 18", "18-24"]) == 0


def test_sounds_wrong_voice_catches_real_review_drafts():
    """Both drafts are verbatim from the 2026-07-14 Ashby review session —
    form answers are pasted as-is, so third-person and 'my profile'
    mechanics-leak must be caught deterministically."""
    from tailoring.resume import sounds_wrong_voice
    assert sounds_wrong_voice(PROFILE, (
        "Tyler's profile is that of a software engineer/developer with no "
        "sales experience mentioned. There is no information in his profile "
        "about coaching someone through a discount request."))
    assert sounds_wrong_voice(PROFILE, (
        "My CRM experience has primarily been with enterprise tools like "
        "ServiceNow. I do not have explicit hands-on experience with "
        "dedicated sales CRMs such as Salesforce listed in my profile."))
    assert sounds_wrong_voice(PROFILE, "The candidate has 3 years of experience.")
    assert not sounds_wrong_voice(PROFILE, (
        "I have worked with ServiceNow and PeopleSoft for workflow and data "
        "management, and I pick up new tools quickly."))


def test_field_answer_redrafts_wrong_voice_once(monkeypatch):
    import tailoring.resume as tr
    drafts = [
        {"answer": "Tyler's profile shows no CRM experience.",
         "confidence": "high", "is_essay": False},
        {"answer": "I have used ServiceNow and PeopleSoft daily.",
         "confidence": "high", "is_essay": False},
    ]
    calls = []
    monkeypatch.setattr(tr, "_ask_json",
                        lambda cfg, system, user: calls.append(user) or drafts[len(calls) - 1])
    res = tr.draft_field_answer({}, PROFILE, "What CRMs have you used?", "", "SWE", "Acme")
    assert res["answer"] == "I have used ServiceNow and PeopleSoft daily."
    assert len(calls) == 2 and "previous draft" in calls[1]


def test_field_answer_unfixed_voice_forces_low_confidence(monkeypatch):
    import tailoring.resume as tr
    bad = {"answer": "Tyler has no such experience in his profile.",
           "confidence": "high", "is_essay": False}
    monkeypatch.setattr(tr, "_ask_json", lambda cfg, system, user: dict(bad))
    res = tr.draft_field_answer({}, PROFILE, "Describe your coaching experience.",
                                "", "SWE", "Acme")
    assert res["confidence"] == "low"  # must be held for review, never auto-sent


def test_country_row_never_shadows_authorization_questions():
    """'Are you authorized to work in the COUNTRY where the job is located?'
    (Ashby/OpenAI Boolean) must answer Yes — a bare \\bcountry\\b pattern
    used to grab it first and answer 'United States' (2026-07-14)."""
    h = _handler()
    assert h.choice_value(
        "Are you authorized to work in the country where the job is located?") == "Yes"
    assert h.choice_value("Country*") == "United States"
    assert h.choice_value("Country of residence") == "United States"


def test_ashby_yesno_unknown_question_held_on_low_confidence():
    import tailoring.resume as tr
    h = _ashby_handler()
    grp = _FakeYesNoGroup("Do you enjoy working weekends?")
    original = tr.draft_choice_answer
    tr.draft_choice_answer = lambda *a, **k: {"choice": "Yes", "confidence": "low"}
    try:
        held = h.handle_yesno_buttons(_FakeAshbyPage(yesno=[grp]))
    finally:
        tr.draft_choice_answer = original
    assert len(held) == 1 and grp.clicked is None


# ---------- Workday: credentials ----------
def _workday_handler(tmp_path=None):
    from handlers.workday import WorkdayHandler
    cfg = {"automation": {"human_delay_ms": [0, 0]},
           "paths": {"pending_dir": "pending"}}
    if tmp_path is not None:
        cfg["paths"]["workday_accounts"] = str(tmp_path / "workday_accounts.json")
    return WorkdayHandler(cfg, PROFILE, _posting(ats="workday"), {"jd_text": ""}, None)


def test_workday_password_meets_policy():
    # Red Hat's tenant demands ≥14 chars (live shakeout 2026-07-18) — the
    # 16-char default must never shrink below that.
    from handlers.workday import WorkdayHandler
    seen = set()
    for _ in range(5):
        pw = WorkdayHandler.generate_password()
        assert len(pw) >= 14
        assert any(c.islower() for c in pw)
        assert any(c.isupper() for c in pw)
        assert any(c.isdigit() for c in pw)
        assert any(c in "!@#$%^&*" for c in pw)
        seen.add(pw)
    assert len(seen) == 5  # never repeats


def test_workday_accounts_roundtrip(tmp_path):
    h = _workday_handler(tmp_path)
    assert h.load_account("acme.wd5.myworkdayjobs.com") is None
    h.save_account("acme.wd5.myworkdayjobs.com", "me@x.com", "Secret1!abcd")
    h.save_account("other.wd1.myworkdayjobs.com", "me@x.com", "Other2@efgh")
    acct = h.load_account("acme.wd5.myworkdayjobs.com")
    assert acct["email"] == "me@x.com" and acct["password"] == "Secret1!abcd"
    assert h.load_account("other.wd1.myworkdayjobs.com")["password"] == "Other2@efgh"
    assert h.load_account("nope.wd2.myworkdayjobs.com") is None
    if sys.platform != "win32":  # plaintext credentials → owner-only
        mode = (tmp_path / "workday_accounts.json").stat().st_mode & 0o777
        assert mode == 0o600


class _FakeClickable:
    def __init__(self, name, log):
        self._name, self._log = name, log

    def is_visible(self):
        return True

    def click(self):
        self._log.append(self._name)


class _FakeAuthPage:
    """Red Hat shakeout 2026-07-18: the real submit <button> sits under a
    click_filter overlay div that intercepts pointer events — the handler
    must click the overlay, not the button."""

    def __init__(self, overlay=True):
        self.clicks = []
        self._overlay = overlay

    def query_selector_all(self, sel):
        if "click_filter" in sel and "Create Account" in sel:
            return [_FakeClickable("overlay", self.clicks)] if self._overlay else []
        if "createAccountSubmitButton" in sel:
            return [_FakeClickable("hidden-button", self.clicks)]
        return []


def test_workday_auth_submit_clicks_overlay_not_hidden_button():
    h = _workday_handler()
    page = _FakeAuthPage(overlay=True)
    assert h._click_submit_control(page, "createAccountSubmitButton",
                                   "Create Account") is True
    assert page.clicks == ["overlay"]


def test_workday_auth_submit_falls_back_to_button_without_overlay():
    h = _workday_handler()
    page = _FakeAuthPage(overlay=False)
    assert h._click_submit_control(page, "createAccountSubmitButton",
                                   "Create Account") is True
    assert page.clicks == ["hidden-button"]


# ---------- Workday: standing answers ----------
def test_workday_choice_state_device_and_phone_code():
    h = _workday_handler()
    assert h.choice_value("State") == "Texas"          # full name, never "TX"
    assert h.choice_value("Phone Device Type*") == "Mobile"
    assert h.choice_value("Country Phone Code") == "United States"
    # shared table must still work through the override
    assert h.choice_value("Veteran Status") == "I am not a protected veteran"
    assert h.choice_value("Country*") == "United States"


def test_workday_previously_worked_answers_no_unless_true():
    h = _workday_handler()
    q = "Have you previously worked for Acme?"
    assert h.choice_value(q) == "No"
    # ...but a company actually in work_history must never auto-answer No
    h.posting.company = "El Paso Water Utilities"
    assert h.choice_value(q) is None


def test_workday_describe_yourself_is_ethnicity():
    h = _workday_handler()
    res = h.answer_choice(
        "How would you describe yourself?",
        ["Asian", "Black or African American", "Hispanic or Latino", "White",
         "I prefer not to disclose"])
    assert res == {"value": "Hispanic or Latino", "hold": False, "source": "profile"}


# ---------- Workday: dropdown widgets (mock-DOM) ----------
class _FakeWdDropdown:
    """button[aria-haspopup='listbox']: text is the chosen value, 'Select One'
    when unanswered; options portal page-wide (see workday.py docstring)."""

    def __init__(self, page, label, options, value=""):
        self._page, self.label = page, label
        self.value = value
        self._options = options

    def is_visible(self):
        return True

    def inner_text(self):
        return self.value or "Select One"

    def click(self):
        self._page.menu = [_FakeOption(t, self._commit) for t in self._options]

    def _commit(self, text):
        self.value = text
        self._page.menu = []

    def get_attribute(self, name):
        return None

    def evaluate(self, js):
        return self.label if "formField" in js else ""


class _FakeWorkdayPage:
    def __init__(self, dropdowns=()):
        self.dropdowns = list(dropdowns)
        self.menu = []
        self.keyboard = _FakeKeyboard()

    def wait_for_timeout(self, ms):
        pass

    def query_selector(self, sel):
        return None

    def query_selector_all(self, sel):
        if "aria-haspopup" in sel:
            return self.dropdowns
        if "promptOption" in sel or "option" in sel:
            return self.menu
        return []


def test_workday_dropdowns_answer_from_profile():
    h = _workday_handler()
    page = _FakeWorkdayPage()
    vet = _FakeWdDropdown(page, "Veteran Status",
                          ["I am not a protected veteran",
                           "I identify as one or more of the classifications "
                           "of a protected veteran", "I don't wish to answer"])
    state = _FakeWdDropdown(page, "State", ["California", "Texas", "Utah"])
    device = _FakeWdDropdown(page, "Phone Device Type", ["Mobile", "Home", "Work"])
    phone_code = _FakeWdDropdown(page, "Country Phone Code",
                                 ["Canada (+1)", "United States of America (+1)"])
    page.dropdowns = [vet, state, device, phone_code]
    held = h.handle_dropdowns(page)
    assert held == []
    assert vet.value == "I am not a protected veteran"
    assert state.value == "Texas"
    assert device.value == "Mobile"
    assert phone_code.value == "United States of America (+1)"


def test_workday_answered_dropdown_left_alone():
    h = _workday_handler()
    page = _FakeWorkdayPage()
    dd = _FakeWdDropdown(page, "Country", ["United States of America", "Canada"],
                         value="United States of America")
    page.dropdowns = [dd]
    assert h.handle_dropdowns(page) == []
    assert dd.value == "United States of America"


def test_workday_unknown_dropdown_held_on_low_confidence():
    import tailoring.resume as tr
    h = _workday_handler()
    page = _FakeWorkdayPage()
    dd = _FakeWdDropdown(page, "Preferred office dog breed", ["Corgi", "Husky"])
    page.dropdowns = [dd]
    original = tr.draft_choice_answer
    tr.draft_choice_answer = lambda *a, **k: {"choice": "Corgi", "confidence": "low"}
    try:
        held = h.handle_dropdowns(page)
    finally:
        tr.draft_choice_answer = original
    assert len(held) == 1 and held[0]["question"] == "Preferred office dog breed"
    assert dd.value == ""  # never guessed


def test_workday_selected_pills_never_treated_as_menu_options():
    """Selected multiselect pills reuse the promptOption automation-id
    (nested under selectedItemList) — the Red Hat run saw the pre-answered
    Country Phone Code pill as a 'menu option'; clicking it would DESELECT
    it. _visible_options must filter pills out."""
    h = _workday_handler()
    page = _FakeWorkdayPage()
    page.menu = [_FakeOption("United States of America (+1)", is_pill=True),
                 _FakeOption("Mobile"), _FakeOption("Home")]
    options = [t for _, t in h._visible_options(page)]
    assert options == ["Mobile", "Home"]


def test_workday_previously_worked_red_hat_phrasing():
    """'Have you ever worked for Red Hat?' (live 2026-07-18) must resolve
    deterministically — but questionnaire asks like 'How long have you
    worked with Python?' must NOT hit this row."""
    h = _workday_handler()
    assert h.choice_value("Have you ever worked for Red Hat?") == "No"
    assert h.choice_value("Have you previously been employed by Acme?") == "No"
    assert h.choice_value("How long have you worked with Python?") is None


def test_workday_address_labels_fill_from_identity():
    h = _workday_handler()
    assert h.standard_value("Address Line 1") == "12253 Delacroix Dr"
    assert h.standard_value("Postal Code") == "79936"
    assert h.standard_value("City") == "El Paso"


class _FakeWdRadio:
    def __init__(self, rid, name):
        self._id, self._name = rid, name
        self.checked = False

    def is_checked(self):
        return self.checked

    def get_attribute(self, attr):
        return {"id": self._id, "name": self._name}.get(attr)

    def evaluate(self, js):
        return "Have you ever worked for Red Hat?*" if "fieldset" in js else ""


class _FakeWdLabel:
    def __init__(self, text, log):
        self._text, self._log = text, log

    def inner_text(self):
        return self._text

    def click(self, **kw):
        self._log.append(self._text)


class _FakeWdRadioPage:
    """Red Hat DOM: radios behind styled spans (check() times out), question
    in fieldset>legend, options as label[for=id]."""

    def __init__(self):
        self.clicks = []
        self.radios = [_FakeWdRadio("cuuy4", "candidateIsPreviousWorker"),
                       _FakeWdRadio("cuuy5", "candidateIsPreviousWorker")]
        self._labels = {"cuuy4": _FakeWdLabel("Yes", self.clicks),
                        "cuuy5": _FakeWdLabel("No", self.clicks)}

    def query_selector_all(self, sel):
        return self.radios if "radio" in sel else []

    def query_selector(self, sel):
        m = re.search(r"label\[for='([^']+)'\]", sel)
        return self._labels.get(m.group(1)) if m else None


def test_workday_radios_answered_via_label_click():
    h = _workday_handler()
    page = _FakeWdRadioPage()
    held = h.handle_wd_radios(page)
    assert held == []
    assert page.clicks == ["No"]  # never worked for Red Hat → label clicked


def test_workday_step_detection_uses_progress_bar():
    """h1/h2 is the job title — identical on every wizard page. The step
    must come from progressBarActiveStep (Red Hat 'stuck' escalation)."""
    h = _workday_handler()

    class _Step:
        def is_visible(self):
            return True

        def inner_text(self):
            return "current step 2 of 7\nMy Information"

    class _Page:
        def query_selector_all(self, sel):
            if "progressBarActiveStep" in sel:
                return [_Step()]
            return []

    assert h._page_step(_Page()) == "current step 2 of 7 My Information"


def test_workday_hear_about_dropdown_resolves_careers():
    import tailoring.resume as tr
    h = _workday_handler()
    page = _FakeWorkdayPage()
    dd = _FakeWdDropdown(page, "How Did You Hear About Us?",
                         ["Job Board", "Referral", "Acme Careers Site", "Other"])
    page.dropdowns = [dd]
    original = tr.draft_choice_answer
    tr.draft_choice_answer = lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("API called"))
    try:
        held = h.handle_dropdowns(page)
    finally:
        tr.draft_choice_answer = original
    assert held == [] and dd.value == "Acme Careers Site"


class _FakeDateSeg:
    def __init__(self, value, react_ok=True):
        self.value = value
        self._react_ok = react_ok  # False = native-setter path doesn't stick

    def input_value(self):
        return self.value

    def evaluate(self, js, arg=None):
        assert "HTMLInputElement" in js  # native setter, not naive el.value
        if self._react_ok:
            self.value = arg

    def click(self, **kw):
        pass

    def press(self, key):
        assert "a" in key.lower()  # select-all before typing

    def type(self, text, delay=0):
        self.value = text


class _FakeCompanyInput:
    def __init__(self, text):
        self._text = text

    def input_value(self):
        return self._text


class _FakeExpPanel:
    """[role=group] panel: company input + date segment inputs addressed by
    '[data-automation-id=formField-X] input[data-automation-id=seg]'."""

    def __init__(self, labelledby, company=None, segs=None):
        self._labelledby = labelledby
        self._company = company
        self.segs = segs or {}  # (field_aid, seg_aid) -> _FakeDateSeg

    def get_attribute(self, name):
        return self._labelledby if name == "aria-labelledby" else None

    def query_selector(self, sel):
        if "companyName" in sel:
            return _FakeCompanyInput(self._company) if self._company else None
        m = re.search(r"formField-(\w+)'\]\s+input\[data-automation-id='([\w-]+)'", sel)
        if m:
            return self.segs.get((f"formField-{m.group(1)}", m.group(2)))
        return None


class _FakeExpPage:
    def __init__(self, panels):
        self._panels = panels

    def query_selector_all(self, sel):
        if "Work-Experience-" in sel:
            return [p for p in self._panels if "Work-Experience" in p._labelledby]
        if "Education-" in sel:
            return [p for p in self._panels if "Education" in p._labelledby]
        return []


def test_workday_experience_dates_rewritten_from_profile():
    """Resume parse gave Lugo 01/2008–01/2019 (real: 08/2019–09/2022) and
    education 2022→2005 (real: 2018→2022) — Red Hat run 2026-07-18."""
    h = _workday_handler()
    lugo = _FakeExpPanel("Work-Experience-2-panel", company="Lugo Speech Therapy",
                         segs={("formField-startDate", "dateSectionMonth-input"): _FakeDateSeg("1"),
                               ("formField-startDate", "dateSectionYear-input"): _FakeDateSeg("2008"),
                               ("formField-endDate", "dateSectionMonth-input"): _FakeDateSeg("1"),
                               ("formField-endDate", "dateSectionYear-input"): _FakeDateSeg("2019")})
    edu = _FakeExpPanel("Education-1-panel",
                        segs={("formField-firstYearAttended", "dateSectionYear-input"): _FakeDateSeg("2022"),
                              ("formField-lastYearAttended", "dateSectionYear-input"): _FakeDateSeg("2005")})
    h.verify_experience_dates(_FakeExpPage([lugo, edu]))
    assert lugo.segs[("formField-startDate", "dateSectionMonth-input")].value == "08"
    assert lugo.segs[("formField-startDate", "dateSectionYear-input")].value == "2019"
    assert lugo.segs[("formField-endDate", "dateSectionMonth-input")].value == "09"
    assert lugo.segs[("formField-endDate", "dateSectionYear-input")].value == "2022"
    assert edu.segs[("formField-firstYearAttended", "dateSectionYear-input")].value == "2018"
    assert edu.segs[("formField-lastYearAttended", "dateSectionYear-input")].value == "2022"


def test_workday_date_rewrite_falls_back_to_keyboard():
    """When the native-setter path doesn't stick, the keyboard path must
    still land the value (Travelers 2026-07-18: typing alone was swallowed
    by the calendar popup — now it's the verified fallback)."""
    h = _workday_handler()
    stubborn = _FakeDateSeg("1", react_ok=False)
    panel = _FakeExpPanel("Work-Experience-2-panel", company="Lugo Speech Therapy",
                          segs={("formField-startDate", "dateSectionMonth-input"):
                                stubborn})
    h.verify_experience_dates(_FakeExpPage([panel]))
    assert stubborn.value == "08"  # keyboard fallback landed it


def test_workday_current_job_and_unknown_companies_untouched():
    h = _workday_handler()
    epw = _FakeExpPanel(
        "Work-Experience-1-panel",
        company="El Paso Water Utilities — Computer Information Systems",
        segs={("formField-startDate", "dateSectionMonth-input"): _FakeDateSeg("10"),
              ("formField-startDate", "dateSectionYear-input"): _FakeDateSeg("2022"),
              ("formField-endDate", "dateSectionMonth-input"): _FakeDateSeg("6")})
    other = _FakeExpPanel("Work-Experience-3-panel", company="Some Other Corp",
                          segs={("formField-startDate", "dateSectionMonth-input"):
                                _FakeDateSeg("3")})
    section = _FakeExpPanel("Work-Experience-section", company="El Paso Water Utilities")
    h.verify_experience_dates(_FakeExpPage([section, epw, other]))
    # current job (end: present) must never get an end date written
    assert epw.segs[("formField-endDate", "dateSectionMonth-input")].value == "6"
    # non-profile employers are never touched
    assert other.segs[("formField-startDate", "dateSectionMonth-input")].value == "3"


def test_workday_authorization_questionnaire_rows():
    """Red Hat Application Questions 2026-07-18 (user: 'I will never need
    authorization to work'): hold-outside and require-authorization answer
    No; plain are-you-authorized keeps answering Yes; relocation always
    Yes; certify-dropdowns answer Yes deterministically."""
    h = _workday_handler()
    assert h.choice_value(
        "Do you hold any work authorization outside of your current location?") == "No"
    assert h.choice_value(
        "Do you require work authorization to work in the country of origin "
        "for this position?") == "No"
    assert h.choice_value(
        "Are you legally authorized to work in the United States?") == "Yes"
    assert h.choice_value("Are you willing to relocate?") == "Yes"
    assert h.choice_value(
        'By selecting "yes" I certify that all information I provide to Red Hat '
        "in connection with my potential employment and thereafter is, and will "
        "be, true and accurate to the best of my knowledge.") == "Yes"


def test_veteran_answer_matches_unprotected_wording():
    """Red Hat's veteran list says 'I am not a veteran' (no 'protected') —
    must resolve deterministically, never prompt."""
    import tailoring.resume as tr
    h = _handler()
    original = tr.draft_choice_answer
    tr.draft_choice_answer = lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("API called"))
    try:
        res = h.answer_choice("Please select the veteran status.",
                              ["I am a veteran", "I am not a veteran",
                               "I prefer not to answer"])
        assert res == {"value": "I am not a veteran", "hold": False,
                       "source": "profile"}
    finally:
        tr.draft_choice_answer = original


class _FakeErrEl:
    def is_visible(self):
        return True

    def inner_text(self):
        return "The field Please select the veteran status. is required"


class _FakeErrPage:
    """Voluntary Disclosures: field-level errorMessage nodes, NO page-level
    banner — must read as 'error' (refill+retry), never 'stuck'."""

    def wait_for_timeout(self, ms):
        pass

    def query_selector_all(self, sel):
        return [_FakeErrEl()] if "errorMessage" in sel else []


def test_workday_field_errors_without_banner_trigger_retry():
    h = _workday_handler()
    assert h._advance_state(_FakeErrPage(), "step 4", timeout_ms=1000) == "error"


def test_veteran_always_picks_plain_not_a_veteran():
    """Travelers lists BOTH 'a veteran, but I am not a protected veteran'
    and 'not a veteran' — containment picked the former because it contains
    the standing answer. Always 'not a veteran' (user 2026-07-18)."""
    import tailoring.resume as tr
    h = _handler()
    original = tr.draft_choice_answer
    tr.draft_choice_answer = lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("API called"))
    try:
        res = h.answer_choice(
            "Veteran Status:",
            ["a protected veteran as defined by the categories above",
             "a veteran, but I am not a protected veteran",
             "not a veteran", "I do not wish to identify"])
        assert res == {"value": "not a veteran", "hold": False, "source": "profile"}
    finally:
        tr.draft_choice_answer = original


def test_workday_travelers_questionnaire_standing_answers():
    """Every question on Travelers' Application Questions page (2026-07-18)
    must resolve deterministically — the whole page fell to the user once."""
    h = _workday_handler()
    h.posting.company = "020 Travelers Indemnity Co"
    assert h.choice_value(
        "Are you currently employed by Travelers (i.e., Travelers issues "
        "your paychecks directly)?") == "No"
    assert h.choice_value("Are you at least 18 years of age or older?") == "Yes"
    assert h.choice_value(
        "Are you legally authorized to work in the country in which you are "
        "applying? (If hired, you will be asked to furnish documents...)") == "Yes"
    assert h.choice_value(
        "Will you now or in the future require support from an employer to "
        "obtain/extend a work visa/permit in order to be employed (for example, "
        "H-1B, TN, Forms I-983/STEM OPT or other employment-based "
        "sponsorship/support)?") == "No"
    assert h.choice_value(
        "Are you currently subject to any agreements with any current and/or "
        "former employer that could restrict your post-employment activities, "
        "such as non-competition or customer or employee non-solicitation "
        "agreements or other contractual clauses?") == "No"
    assert h.choice_value(
        "Have you ever been involuntarily discharged or asked to resign from "
        "a position?") == "No"


def test_workday_auth_page_never_mistaken_for_wizard():
    """Travelers renders the progress bar ON the auth page (step 1 is
    'Create Account/Sign In') and it appears before the form inputs — a
    visible password field must always mean auth, not wizard
    (2026-07-18: skipped authentication and hunted for a Save button)."""
    h = _workday_handler()

    class _Pw:
        def is_visible(self):
            return True

    class _AuthPage:
        def query_selector(self, sel):
            return object() if "progressBar" in sel else None

        def query_selector_all(self, sel):
            return [_Pw()] if "password" in sel else []

    class _WizardPage:
        def query_selector(self, sel):
            return object() if "progressBar" in sel else None

        def query_selector_all(self, sel):
            return []

    assert h._on_wizard(_AuthPage()) is False   # progress bar + password = auth
    assert h._on_wizard(_WizardPage()) is True  # progress bar, no password


def test_workday_loop_guard_stops_repeated_same_page_passes(monkeypatch):
    """Spurious 'advanced' states re-ran full fill passes on the same page —
    visible as endless field scrolling (user 2026-07-18). After 3 passes
    without progress the run must escalate, not spin."""
    from handlers.base import RunResult
    h = _workday_handler()
    fills = []
    btn = type("B", (), {"inner_text": lambda s: "Save and Continue",
                         "click": lambda s: None})()
    monkeypatch.setattr(h, "_reach_application", lambda p: None)
    monkeypatch.setattr(h, "_maybe_email_verification", lambda p: None)
    monkeypatch.setattr(h, "_next_button", lambda p, **k: btn)
    monkeypatch.setattr(h, "_wait_form_settle", lambda p: None)
    monkeypatch.setattr(h, "_upload_resume_if_asked", lambda p: None)
    monkeypatch.setattr(h, "fill_page", lambda p: fills.append(1) or [])
    monkeypatch.setattr(h, "_page_step",
                        lambda p: "current step 4 of 6 Voluntary Disclosures")
    monkeypatch.setattr(h, "_advance_state", lambda p, prev, **k: "advanced")
    monkeypatch.setattr(h, "escalate_now",
                        lambda p, reason, extra=None:
                        RunResult(status="escalated", reason=reason))
    res = h._attempt(object())
    assert res.status == "escalated" and "Loop guard" in res.reason
    assert len(fills) <= 2  # one repeat pass max (halved per user 2026-07-18)


def test_workday_offer_resume_prompt(monkeypatch):
    """Escalations pause for an in-run retry with a terminal (user
    2026-07-18); 's' or no TTY = stop (old behavior, phone runs safe)."""
    import sys as _sys
    from handlers.base import RunResult
    h = _workday_handler()
    res = RunResult(status="escalated", reason="test", details={})
    monkeypatch.setattr(_sys.stdin, "isatty", lambda: False)
    assert h._offer_resume(res) is False       # phone run: never hangs
    monkeypatch.setattr(_sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *a: "")
    assert h._offer_resume(res) is True        # Enter = retry
    monkeypatch.setattr("builtins.input", lambda *a: "s")
    assert h._offer_resume(res) is False       # s = stop


def test_workday_apply_retries_after_escalation(monkeypatch):
    from handlers.base import RunResult

    class _P:
        def goto(self, url, **kw):
            pass

        def wait_for_timeout(self, ms):
            pass

        def query_selector_all(self, sel):
            return []

    h = _workday_handler()
    outcomes = [RunResult(status="escalated", reason="first try"),
                RunResult(status="submitted", reason="second try")]
    monkeypatch.setattr(h, "_attempt", lambda page: outcomes.pop(0))
    monkeypatch.setattr(h, "_offer_resume", lambda res: True)
    result = h.apply(_P())
    assert result.status == "submitted" and outcomes == []


# ---------- Workday: CXS intake enrichment ----------
def test_workday_api_url_built_from_posting_url():
    from intake.fetcher import company_from_workday_url, workday_api_url
    assert workday_api_url(
        "https://acme.wd5.myworkdayjobs.com/en-US/careers/job/El-Paso-TX/"
        "Software-Engineer_JR-123") == \
        ("https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/careers/job/"
         "El-Paso-TX/Software-Engineer_JR-123")
    # locale segment is optional
    assert workday_api_url(
        "https://acme.wd1.myworkdayjobs.com/ext/job/Remote/Analyst_R99") == \
        "https://acme.wd1.myworkdayjobs.com/wday/cxs/acme/ext/job/Remote/Analyst_R99"
    assert workday_api_url("https://boards.greenhouse.io/acme/jobs/1") is None
    assert company_from_workday_url(
        "https://acme.wd5.myworkdayjobs.com/en-US/careers/job/x") == "Acme"


class _FakeJsonResp:
    def __init__(self, url, text="", payload=None):
        self.url, self.text = url, text
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def test_workday_intake_enriched_from_cxs_api(monkeypatch):
    import intake.fetcher as f
    job_url = ("https://acme.wd5.myworkdayjobs.com/en-US/careers/job/El-Paso-TX/"
               "Software-Engineer_JR-123")

    def fake_get(url, **kw):
        if "/wday/cxs/" in url:
            return _FakeJsonResp(url, payload={
                "jobPostingInfo": {
                    "title": "Software Engineer",
                    "jobDescription": "<p>Build <b>Python</b> pipelines.</p>",
                    "location": "El Paso, TX"},
                "hiringOrganization": {"name": "Acme Corp"}})
        return _FakeJsonResp(url, text="<html><title>loading…</title></html>")

    monkeypatch.setattr(f.requests, "get", fake_get)
    p = f.fetch_posting(job_url)
    assert p.ats == "workday"
    assert p.title == "Software Engineer"
    assert p.company == "Acme Corp"
    assert "Build Python pipelines." in p.description
    assert p.location == "El Paso, TX"


def test_workday_intake_survives_cxs_failure(monkeypatch):
    """CXS errors must degrade to the generic extraction, never crash."""
    import intake.fetcher as f
    job_url = "https://acme.wd5.myworkdayjobs.com/en-US/careers/job/x/Analyst_R1"

    def fake_get(url, **kw):
        if "/wday/cxs/" in url:
            raise RuntimeError("boom")
        return _FakeJsonResp(url, text="<html><h1>Analyst</h1></html>")

    monkeypatch.setattr(f.requests, "get", fake_get)
    p = f.fetch_posting(job_url)
    assert p.ats == "workday" and p.title == "Analyst"
    assert p.company == "Acme"  # tenant-slug fallback
    assert any("CXS" in w for w in p.warnings)
