"""Base form-automation handler (Playwright).

Shared machinery: persistent browser profile, human-like pacing, standard
field mapping from the profile store, CAPTCHA detection, essay policy,
supervised-mode pause, and submission verification hooks.
"""
from __future__ import annotations

import os
import random
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from escalation.queue import escalate, notify
from tailoring import resume as tailor

CAPTCHA_MARKERS = ["recaptcha", "hcaptcha", "h-captcha", "cf-turnstile", "captcha"]

# Placeholder option texts that mean "nothing selected yet"
_PLACEHOLDER_OPTION = re.compile(r"^(--+|select|please select|choose|pick one)\b|^\s*$", re.I)

# Optional choice fields to leave untouched entirely — never answered, never
# held. Suffix is optional and Tyler has none (2026-07-19); answering/holding
# it just stalls the run.
_IGNORE_CHOICE_RE = re.compile(r"\bsuffix\b", re.I)

# "How did you hear about us?" in any phrasing
_HEAR_ABOUT_RE = re.compile(r"how did you (hear|find)|hear about|referr", re.I)

# Employee-referral questions — Tyler has NO contacts at other companies
# (user directive 2026-07-21). The referrer's name is left BLANK (free-text)
# and yes/no "were you referred by an employee?" answers No. This is
# deliberately NARROWER than _HEAR_ABOUT_RE's bare "referr": a "How did you
# hear about us?" question that merely lists a "Referral" option is still a
# source question, not an employee-contact ask, and must keep resolving to
# the careers page — so employee-referral is excluded from that fallback.
_EMPLOYEE_REFERRAL_RE = re.compile(
    r"referred\s+by\b"                        # "Were you referred by ...?"
    r"|\breferrer\b"                          # "Referrer name"
    r"|who\s+referred\s+you"                  # "Who referred you?"
    r"|\breferr\w*\b[^?]{0,30}\bemployee\b"   # "referral ... employee"
    r"|\bemployee\b[^?]{0,30}\breferr\w*\b"   # "employee ... referral"
    r"|name[^?]{0,30}\breferr\w*"             # "name of the person who referred you"
    r"|know\s+(any|some)one\s+(who\s+(currently\s+)?works?|employed|at\b)",  # "know anyone at ..."
    re.I)


@dataclass
class RunResult:
    status: str                    # submitted | held | escalated | failed
    reason: str = ""
    details: dict = field(default_factory=dict)


class BaseHandler:
    ats_name = "base"

    def __init__(self, cfg: dict, profile: dict, posting, documents: dict, tracker):
        """documents: {"resume": Path, "cover_letter": Path, "jd_text": str, "track": str}"""
        self.cfg = cfg
        self.profile = profile
        self.posting = posting
        self.documents = documents
        self.tracker = tracker
        self.auto = cfg["automation"]

    # ---------- browser lifecycle ----------
    def launch(self, playwright):
        profile_dir = Path(self.cfg["paths"]["browser_profile"]).absolute()
        profile_dir.mkdir(parents=True, exist_ok=True)
        return playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=self.auto.get("headless", False),
            viewport={"width": 1366, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )

    def pause(self):
        lo, hi = self.auto.get("human_delay_ms", [400, 1400])
        time.sleep(random.uniform(lo, hi) / 1000)

    # ---------- shared building blocks ----------
    def detect_captcha(self, page) -> bool:
        """True only when a captcha the user must SOLVE is visibly present.

        Invisible reCAPTCHA infrastructure (script tag + hidden badge) is on
        virtually every Greenhouse form and does not block submission — its
        anchor iframe has 'size=invisible' in the URL. We escalate only for:
        a visible v2 checkbox (anchor iframe without size=invisible), an open
        challenge popup (bframe), or visible hCaptcha/Turnstile widgets.
        """
        selectors = [
            "iframe[src*='recaptcha'][src*='bframe']",                          # open challenge
            "iframe[src*='recaptcha'][src*='anchor']:not([src*='invisible'])",  # v2 checkbox
            "iframe[src*='hcaptcha']",
            "iframe[src*='turnstile']",
        ]
        for sel in selectors:
            for el in page.query_selector_all(sel):
                try:
                    if not el.is_visible():
                        continue
                    box = el.bounding_box()
                    if box and box["width"] > 50 and box["height"] > 50:
                        return True
                except Exception:
                    continue
        return False

    def fill_if_present(self, page, selector: str, value: str | None) -> bool:
        if value is None:
            return False
        el = page.query_selector(selector)
        if el:
            el.fill(str(value))
            self.pause()
            return True
        return False

    @staticmethod
    def _visible(page, selector: str):
        """First VISIBLE element matching `selector`, or None. Forms often
        carry hidden twins of real controls (hcaptcha submit buttons etc.)."""
        for el in page.query_selector_all(selector):
            try:
                if el.is_visible():
                    return el
            except Exception:
                continue
        return None

    def standard_value(self, label_text: str) -> str | None:
        """Map a form label to identity / standard_answers data, if we can."""
        ident, std = self.profile["identity"], self.profile["standard_answers"]
        label = label_text.lower()
        table = [
            (r"first\s*name", ident["first_name"]),
            (r"last\s*name|surname", ident["last_name"]),
            (r"full\s*name|^name\b", ident["full_name"]),
            (r"e-?mail", ident["email"]),
            (r"phone|mobile", ident["phone"]),
            (r"\bcountry\b", ident.get("country")),
            (r"linkedin", ident["linkedin"]),
            (r"github", ident["github"]),
            (r"twitter|\bx\b.*(url|profile|handle)", ident.get("twitter")),
            (r"portfolio|website", ident.get("portfolio")),
            (r"city", ident["city"]),
            (r"location", ident["location"]),
            (r"notice\s*period", std.get("notice_period")),
            (r"preferred\s*(method\s*of\s*)?contact|(preferred\s*)?contact\s*method|"
             r"how.*prefer.*(be\s*)?contact", std.get("preferred_contact_method")),
            # Compensation/salary free-text fields → "Negotiable" (per Tyler
            # 2026-07-19). Numeric-only inputs are excluded (a text value throws
            # on type=number), matching the start-date year/month handling.
            (r"salary|compensation|desired\s*pay|pay\s*expectation|"
             r"expected\s*(pay|rate|compensation)|desired\s*rate",
             (std.get("salary_expectation") or {}).get("text_answer")),
            # Education-section "Start date year"/"month" fields must never
            # get the earliest-start-date sentence (type=number inputs throw
            # on text) — hence the year/month lookahead exclusion.
            (r"(?:start\s*date|available)(?!.*\b(?:year|month)\b)",
             std.get("earliest_start_date")),
        ]
        for pattern, value in table:
            if re.search(pattern, label):
                v = str(value) if value is not None else None
                return None if (v and v.startswith("TODO")) else v
        return None

    # ---------- choice questions (selects / radio groups) ----------
    def choice_value(self, question: str) -> str | None:
        """Deterministic answer for a choice-style question, from standard_answers.

        Returns the *desired answer text* (still needs matching against the
        actual options) or None if the question isn't recognized.
        """
        std = self.profile["standard_answers"]
        q = question.lower()

        # Employee-referral yes/no ("Were you referred by an employee?") → No
        # (user 2026-07-21: no contacts at other companies). Checked before the
        # hear-about row below so its "referr" pattern can't answer it with the
        # careers-page source value.
        if _EMPLOYEE_REFERRAL_RE.search(question):
            return "No"

        def yn(flag) -> str:
            return "Yes" if flag else "No"

        table = [
            # Label-style asks only ("Country*", "Country of residence") —
            # a bare \bcountry\b also matched "Are you authorized to work
            # in the COUNTRY where the job is located?" (Ashby/OpenAI
            # Boolean, 2026-07-14) and answered it "United States",
            # shadowing the authorization row below.
            (r"^\W*country\b\W*$|country of (residence|citizenship)",
             self.profile["identity"].get("country")),
            # Nepotism/relationship disclosure — "Are you related to, or in a
            # close personal relationship with, anyone who works for <company>?"
            # → No (SIE run 2026-07-21; Tyler has no relatives/partners at other
            # companies, consistent with the no-contacts referral directive).
            # Requires an employee/works-for context so it can't catch a
            # "describe your working relationship" essay.
            (r"(?:\brelated to\b|personal relationship|\brelative\b|"
             r"family member)[^?]{0,120}(work|employ|studio|compan|organi|staff)",
             "No"),
            (r"sponsor", yn(std.get("require_sponsorship", False))),
            (r"non.?comp|non.?solicit|agreement .*(restrict|former employer)",
             yn(std.get("has_noncompete", False))),
            (r"opt.?in .*(text|sms|messag)|text messag.*opt",
             "Yes" if std.get("sms_opt_in", True) else "No"),
            # MUST precede the generic authoriz row (Red Hat Workday
            # questionnaire 2026-07-18, user: "I will never need
            # authorization to work" — US citizen):
            #   "Do you HOLD any work authorization OUTSIDE of your current
            #    location?" → No
            #   "Do you REQUIRE work authorization ...?" → No
            (r"authoriz\w*.{0,50}outside|outside.{0,50}authoriz", "No"),
            (r"(require|need)\w*.{0,40}authoriz",
             yn(std.get("require_sponsorship", False))),
            (r"authoriz|legally.*work|right\s*to\s*work|eligib.*work", "Yes"),
            (r"relocat", yn(std.get("willing_to_relocate", True))),
            # "By selecting yes I certify ... true and accurate" dropdowns —
            # the dropdown twin of a consent checkbox (Red Hat 2026-07-18)
            (r"certify.{0,120}(true|accurate)", "Yes"),
            (r"veteran", std.get("veteran_status")),
            (r"disabilit", std.get("disability_status")),
            (r"hispanic|latino|latinx",
             yn("hispanic" in str(std.get("race_ethnicity", "")).lower())),
            (r"race|ethnicit", std.get("race_ethnicity")),
            # "Which communities do you belong to?"-style demographic surveys
            # (Ashby) — per Tyler 2026-07-14: prefer not to answer.
            (r"communit(?:y|ies).*belong|belong.*communit",
             std.get("demographic_communities")),
            (r"pronoun", std.get("pronouns")),
            # Sexual Orientation → Heterosexual (SIE run 2026-07-21; the Claude
            # fallback had suggested "I don't wish to answer"). Above the gender
            # row for clarity — "sexual" won't trip \bsex\b, but keep it first.
            (r"sexual orientation", std.get("sexual_orientation")),
            # MUST precede the gender row: "transgender" contains "gender"
            # and would otherwise be answered with the gender value.
            (r"transgender", std.get("transgender")),
            (r"gender|\bsex\b", std.get("gender")),
            (r"\b18\b|age\s*of\s*majority|legal\s*age", yn(std.get("over_18", True))),
            # MUST stay below the over-18 row ("...18 years of AGE?").
            # Range options ("25-34") resolve via age_option_index.
            (r"\bage\b|how old", std.get("age")),
            (r"preferred\s*(method\s*of\s*)?contact|(preferred\s*)?contact\s*method|"
             r"how.*prefer.*(be\s*)?contact", std.get("preferred_contact_method")),
            (r"how did you (hear|find)|hear about|referr", std.get("how_did_you_hear")),
            (r"felony|criminal|convict", std.get("criminal_history")),
            # "Have you ever been involuntarily discharged or asked to
            # resign?" (Travelers questionnaire 2026-07-18)
            (r"involuntar\w*\s+discharg|asked to resign|ever been (fired|terminated)",
             "No"),
            (r"citizen", std.get("citizenship")),
        ]
        for pattern, value in table:
            if re.search(pattern, q):
                v = str(value) if value is not None else None
                return None if (v and v.startswith("TODO")) else v
        return None

    @staticmethod
    def _norm(s: str) -> str:
        return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower())).strip()

    def match_option(self, desired: str | None, options: list[str]) -> int | None:
        """Best-effort match of a desired answer against option texts."""
        d = self._norm(str(desired)) if desired is not None else ""
        if not d:
            return None
        norm = [self._norm(o) for o in options]
        for i, o in enumerate(norm):
            if o == d:
                return i
        if d in ("yes", "no", "true", "false"):
            want = "yes" if d in ("yes", "true") else "no"
            for i, o in enumerate(norm):
                if o == want or o.startswith(want + " ") or o.startswith(want + ","):
                    return i
            return None  # never substring-match a bare yes/no — too risky
        # Substring matches: when several options contain the desired string
        # ("United States" is a prefix of both "United States of America" and
        # "United States Minor Outlying Islands"), returning the first hit
        # picks whichever sorts earliest — wrong. Prefer the option closest in
        # length to the desired text (the tightest superset).
        containing = [i for i, o in enumerate(norm) if d in o]
        if containing:
            return min(containing, key=lambda i: len(norm[i]))
        contained = [i for i, o in enumerate(norm) if o in d]
        if contained:
            return max(contained, key=lambda i: len(norm[i]))
        # Space-insensitive last resort ("US citizen" vs "U.S. Citizen")
        d_tight = d.replace(" ", "")
        tight = [i for i, o in enumerate(norm)
                 if d_tight in o.replace(" ", "") or o.replace(" ", "") in d_tight]
        if tight:
            return min(tight, key=lambda i: abs(len(norm[i].replace(" ", "")) - len(d_tight)))
        return None

    _STOP_TOKENS = {"the", "of", "at", "in", "and"}

    @classmethod
    def _sig_tokens(cls, s: str) -> set[str]:
        return {w for w in cls._norm(s).split() if w not in cls._STOP_TOKENS}

    def fuzzy_index(self, desired: str, options: list[str]) -> int | None:
        """Significant-token matching for proper nouns whose option text is
        formatted differently ('The University of Texas at El Paso' vs
        'University Texas - El Paso'; 'El Paso, TX' vs 'El Paso, Texas,
        United States'). Never used for yes/no-style answers — callers opt
        in only for school/location pickers."""
        want = self._sig_tokens(desired)
        if len(want) < 2:
            return None
        for i, o in enumerate(options):
            if want <= self._sig_tokens(o):
                return i
        for i, o in enumerate(options):
            have = self._sig_tokens(o)
            if len(have) >= 3 and have <= want:
                return i
        return None

    def careers_option_index(self, options: list[str]) -> int | None:
        """'How did you hear about us' must always resolve without stopping
        (user directive 2026-07-14): option text varies per company
        ("Smartsheet Careers Site", "Careers Page", "Company Website"), so
        exact matching against the profile's "Company careers page" fails.
        Preference order: careers page/site/website → company website →
        jobs page/site ("Red Hat Jobs Site", Workday 2026-07-18 — "Job
        Board" stays excluded: no page/site tail) → anything careers-ish
        that isn't a career FAIR → "Other"."""
        norm = [self._norm(o) for o in options]
        for pattern in (r"career.*\b(page|site|website|web site)\b",
                        r"company (website|web site)",
                        r"\bjobs?\b.*\b(page|site|website|web site)\b",
                        r"\bcareers?\b(?!.*fair)",
                        r"^other$"):
            for i, o in enumerate(norm):
                if re.search(pattern, o):
                    return i
        return None

    def ethnicity_option_index(self, options: list[str]) -> int | None:
        """Hispanic/Latino option text varies per form ("Hispanic or Latino",
        "Hispanic or Latine", "Latinx", "Hispanic/Latina/o") — exact matching
        missed "Hispanic or Latine" on Ashby's demographic survey and the
        Claude fallback picked "I prefer not to answer" (user 2026-07-14:
        ethnicity questions ALWAYS answer Hispanic/Latino). Fires only when
        the desired answer itself is Hispanic/Latino-flavored.

        NEGATED mentions don't count: Red Hat (Workday, 2026-07-18) labels
        every non-Hispanic race "... (Not Hispanic or Latino)", which made
        'American Indian or Alaska Native (Not Hispanic or Latino)' the
        first "match"."""
        for i, o in enumerate(options):
            cleaned = re.sub(r"\b(not|non)[\s-]+(hispanic|latin[oaex])[^)]*",
                             "", o, flags=re.I)
            if re.search(r"hispanic|latin[oaex]", cleaned, re.I):
                return i
        return None

    @staticmethod
    def _age_bounds(option: str) -> tuple[int, int] | None:
        """(lo, hi) for a range-style age option, or None. Handles "25-34",
        "25 to 34", "35+", "35 or older", "Under 18"."""
        o = option.lower()
        nums = [int(n) for n in re.findall(r"\d+", o)]
        if not nums:
            return None
        if len(nums) >= 2:
            return (min(nums), max(nums))
        n = nums[0]
        if re.search(r"under|younger|below|less than", o):
            return (0, n - 1)
        if re.search(r"\+|or older|and older|\bover\b|above|older than", o):
            return (n, 200)
        return (n, n)

    def veteran_option_index(self, options: list[str]) -> int | None:
        """Veteran questions always answer 'not a veteran' (user 2026-07-18).
        Plain containment matched 'a veteran, but I am not a protected
        veteran' on Travelers (the standing answer is a substring of it) —
        prefer a plain not-a-veteran option (no 'protected'), then any
        not-…-veteran variant ('I am not a protected veteran', Red Hat)."""
        norm = [self._norm(o) for o in options]
        for i, o in enumerate(norm):
            if re.search(r"\bnot a veteran\b", o) and "protected" not in o:
                return i
        for i, o in enumerate(norm):
            if re.search(r"\bnot\b.*\bveteran\b", o):
                return i
        return None

    def age_option_index(self, age: int, options: list[str]) -> int | None:
        """Pick the range option containing `age` ("26" never text-matches
        "25-34")."""
        for i, o in enumerate(options):
            bounds = self._age_bounds(o)
            if bounds and bounds[0] <= age <= bounds[1]:
                return i
        return None

    def answer_choice(self, question: str, options: list[str]) -> dict:
        """Pick one of `options` for `question`.

        Deterministic mapping from standard_answers first; Claude fallback
        second. Returns {"value": str|None, "hold": bool, "source": str}.
        When hold is True the caller must NOT fill — escalate for review.
        """
        desired = self.choice_value(question)
        if desired is not None:
            idx = None
            # Ethnicity desired answers need the negation-aware matcher
            # FIRST: generic substring matching finds "Hispanic or Latino"
            # inside "... (Not Hispanic or Latino)" (Red Hat 2026-07-18).
            if re.search(r"hispanic|latin[oaex]", desired, re.I):
                idx = self.ethnicity_option_index(options)
                # US EEO forms split ethnicity (Hispanic/Latino) from RACE:
                # the SIE race question (2026-07-21) offered White/Black/
                # Asian/… with NO Hispanic option, so ethnicity_option_index
                # found nothing and the answer fell to Claude ("I don't wish
                # to answer"). Fall back to the race standing answer (White).
                if idx is None:
                    race = self.profile["standard_answers"].get("race")
                    if race:
                        idx = self.match_option(race, options)
            # Veteran also needs its specialized matcher BEFORE containment:
            # "a veteran, but I am not a protected veteran" CONTAINS the
            # standing answer, so containment picked it (Travelers
            # 2026-07-18; user: always answer "not a veteran").
            if idx is None and re.search(r"not.*veteran", desired, re.I):
                idx = self.veteran_option_index(options)
            if idx is None:
                idx = self.match_option(desired, options)
            if idx is None and _HEAR_ABOUT_RE.search(question) \
                    and not _EMPLOYEE_REFERRAL_RE.search(question):
                idx = self.careers_option_index(options)
            if idx is None and desired.isdigit() \
                    and re.search(r"\bage\b|how old", question, re.I):
                idx = self.age_option_index(int(desired), options)
            if idx is not None:
                return {"value": options[idx], "hold": False, "source": "profile"}
        try:
            draft = tailor.draft_choice_answer(
                self.cfg, self.profile, question, options,
                self.documents.get("jd_text", ""), self.posting.title, self.posting.company,
            )
        except Exception as exc:
            return {"value": None, "hold": True, "source": f"error: {exc}"}
        idx = self.match_option(draft.get("choice"), options)
        hold = draft.get("confidence") == "low" or idx is None
        if hold and self.auto.get("essay_policy", "hold") == "review":
            picked = self.review_choice(
                question, options, options[idx] if idx is not None else None)
            if picked is not None:
                return {"value": picked, "hold": False, "source": "reviewed"}
        return {"value": options[idx] if idx is not None else None,
                "hold": hold, "source": "claude"}

    def field_label(self, page, el) -> str:
        """Generic label finder: label[for], aria-label, container text, name."""
        el_id = el.get_attribute("id")
        if el_id:
            lab = page.query_selector(f"label[for='{el_id}']")
            if lab:
                text = (lab.inner_text() or "").strip()
                if text:
                    return text
        for attr in ("aria-label", "placeholder"):
            v = el.get_attribute(attr)
            if v and v.strip():
                return v.strip()
        try:
            text = el.evaluate(
                "el => { const c = el.closest('label, fieldset, [class*=question], [class*=field]');"
                " if (!c) return ''; const lg = c.querySelector('legend, label, [class*=label]');"
                " return (lg ? lg.innerText : c.innerText) || ''; }")
            text = (text or "").split("\n")[0].strip()
            if text:
                return text
        except Exception:
            pass
        return (el.get_attribute("name") or "").strip()

    def handle_native_selects(self, page) -> list[dict]:
        """Fill unanswered single-choice <select> elements. Returns held items."""
        held = []
        for sel in page.query_selector_all("select"):
            try:
                if sel.get_attribute("multiple") is not None:
                    continue
                if sel.evaluate("el => !!el.value"):
                    continue  # already answered
                pairs = [((o.get_attribute("value") or ""), (o.text_content() or "").strip())
                         for o in sel.query_selector_all("option")]
            except Exception:
                continue
            real = [(v, t) for v, t in pairs if v and not _PLACEHOLDER_OPTION.match(t)]
            if not real:
                continue
            question = self.field_label(page, sel)
            if not question:
                continue
            if _IGNORE_CHOICE_RE.search(question):
                continue  # optional field left blank (e.g. Suffix)
            res = self.answer_choice(question, [t for _, t in real])
            if res["hold"] or res["value"] is None:
                held.append({"question": question, "options": [t for _, t in real],
                             "draft_answer": res["value"] or ""})
                continue
            value = next(v for v, t in real if t == res["value"])
            try:
                sel.select_option(value=value)
            except Exception:
                # Hidden native select behind a JS widget (e.g. select2):
                # set the value directly and fire the events frameworks listen for.
                sel.evaluate(
                    "(el, v) => { el.value = v;"
                    " el.dispatchEvent(new Event('input',  {bubbles: true}));"
                    " el.dispatchEvent(new Event('change', {bubbles: true})); }", value)
            self.pause()
        return held

    def handle_react_selects(self, page) -> list[dict]:
        """Fill react-select comboboxes (new job-boards.greenhouse.io UI).

        Structure: <input class="select__input" role="combobox" id=X> with
        label[for=X]; a chosen value renders as .select__single-value in the
        container. Options ([role=option]) only exist while the menu is open.
        """
        held = []
        for inp in page.query_selector_all("input.select__input[role='combobox']"):
            try:
                filled = inp.evaluate(
                    "el => { const c = el.closest('.select__container, .select-shell, .select');"
                    " return !!(c && c.querySelector('.select__single-value, .select__multi-value')); }")
                if filled:
                    continue
                question = self.field_label(page, inp)
                if not question:
                    continue
                inp.click()
                page.wait_for_timeout(500)
                # Scope options to THIS control's menu — react-select ids them
                # 'react-select-<inputId>-option-N'. A page-wide [role=option]
                # query also catches the phone widget's country list.
                inp_id = inp.get_attribute("id") or ""
                opts = page.query_selector_all(
                    f"[id^='react-select-{inp_id}-option']") if inp_id else []
                if not opts:
                    cont = inp.evaluate_handle(
                        "el => el.closest('.select__container, .select-shell, .select')"
                    ).as_element()
                    opts = cont.query_selector_all(
                        ".select__option, [role='option']") if cont else []
                pairs = [(o, (o.inner_text() or "").strip())
                         for o in opts if o.is_visible()]
                pairs = [(o, t) for o, t in pairs if t]
                if not pairs:
                    page.keyboard.press("Escape")
                    continue
                options = [t for _, t in pairs]
                res = self.answer_choice(question, options)
                if res["hold"] or res["value"] is None:
                    held.append({"question": question, "options": options[:30],
                                 "draft_answer": res["value"] or ""})
                    page.keyboard.press("Escape")
                    continue
                for o, t in pairs:
                    if t == res["value"]:
                        o.click()
                        self.pause()
                        break
                else:
                    page.keyboard.press("Escape")
            except Exception:
                try:
                    page.keyboard.press("Escape")
                except Exception:
                    pass
                continue
        return held

    def handle_radio_groups(self, page) -> list[dict]:
        """Answer unanswered radio groups. Returns held items."""
        return self._handle_choice_groups(page, "radio")

    def handle_checkbox_groups(self, page) -> list[dict]:
        """Answer choice questions rendered as same-name CHECKBOX groups —
        e.g. Lever's standard pronouns widget (10 checkboxes named
        'pronouns' plus a hidden Custom text field). Single checkboxes are
        left alone: those are the consent handler's territory. Exactly one
        option gets checked — the matched answer."""
        return self._handle_choice_groups(page, "checkbox")

    def _handle_choice_groups(self, page, input_type: str) -> list[dict]:
        held = []
        groups: dict[str, list] = {}
        for r in page.query_selector_all(f"input[type='{input_type}']"):
            name = r.get_attribute("name") or ""
            if name:
                groups.setdefault(name, []).append(r)
        for name, els in groups.items():
            try:
                if input_type == "checkbox" and len(els) < 2:
                    continue  # singleton checkboxes are consent/opt-in, not choices
                if any(e.is_checked() for e in els):
                    continue
                labeled = []
                for e in els:
                    text = e.evaluate(
                        "el => { const l = el.closest('label') ||"
                        " (el.id && document.querySelector(`label[for=\"${el.id}\"]`));"
                        " return (l ? l.innerText : el.value) || ''; }")
                    labeled.append((e, (text or "").strip()))
                options = [t for _, t in labeled if t]
                if not options:
                    continue
                # Find the QUESTION text: must never be one of the option
                # labels. (Lever nests options in a [class*=field] wrapper
                # whose first label is "Yes" — walking to the nearest
                # container and grabbing its first label answered every
                # radio question with its first option.)
                question = els[0].evaluate(
                    """el => {
                        const opts = new Set();
                        for (const r of document.getElementsByName(el.name)) {
                            const l = r.closest('label') ||
                                (r.id && document.querySelector(`label[for="${r.id}"]`));
                            if (l) opts.add(l.innerText.trim());
                        }
                        const f = el.closest('fieldset');
                        const lg = f && f.querySelector('legend');
                        if (lg && !opts.has(lg.innerText.trim())) return lg.innerText;
                        for (const sel of ['[role="radiogroup"]',
                                           '[class*=question]', '[class*=field]']) {
                            const c = el.closest(sel);
                            if (!c) continue;
                            for (const lb of c.querySelectorAll(
                                    'label, legend, [class*=label]')) {
                                const t = (lb.innerText || '').trim();
                                if (t && !opts.has(t) && !lb.querySelector('input'))
                                    return t;
                            }
                        }
                        return '';
                    }""")
                question = " ".join((question or "").split()).strip()
                # No identifiable question, or the "question" is itself one of
                # the options → never guess; hold for review instead.
                if not question or question in options:
                    held.append({"question": question or name, "options": options,
                                 "draft_answer": ""})
                    continue
                res = self.answer_choice(question, options)
                if res["hold"] or res["value"] is None:
                    held.append({"question": question, "options": options,
                                 "draft_answer": res["value"] or ""})
                    continue
                for e, t in labeled:
                    if t == res["value"]:
                        if e.is_visible():
                            e.check()
                        else:
                            # Styled widgets hide the input behind its label;
                            # check() would time out exactly like fill() does.
                            e.evaluate("el => (el.closest('label') || el).click()")
                        self.pause()
                        break
            except Exception:
                continue
        return held

    _CONSENT_RE = re.compile(
        r"i (agree|certify|acknowledge|consent|understand)|by (clicking|submitting)"
        # "Yes, I have read and consent to the terms and conditions."
        # (justfab Workday 2026-07-19) — neither "i consent" adjacency nor
        # "terms of service" matched, so the required box stayed unticked.
        r"|(read|reviewed) and (consent|agree)|terms and conditions"
        r"|privacy policy|terms of service|statements .* true", re.IGNORECASE)

    def handle_consent_checkboxes(self, page) -> None:
        """Tick required agreement checkboxes (privacy policy, certification).

        Only checkboxes whose surrounding text reads like a submission
        agreement are ticked — marketing/newsletter opt-ins are left alone.
        """
        for cb in page.query_selector_all("input[type='checkbox']"):
            try:
                if cb.is_checked():
                    continue
                # Climb ancestors until one carries real text: the checkbox's
                # own label is just "Submit Application" — the agreement
                # sentence lives on the question wrapper a few levels up.
                # Stop before form-level containers so one consent phrase
                # elsewhere on the page can't approve unrelated checkboxes.
                context = cb.evaluate(
                    """el => {
                        let c = el.parentElement;
                        for (let i = 0; i < 6 && c; i++, c = c.parentElement) {
                            const t = (c.innerText || '').trim();
                            if (t.length > 900) break;
                            if (t.length > 40) return t;
                        }
                        return '';
                    }""") or ""
                context = " ".join(context.split())
                if self._CONSENT_RE.search(context):
                    try:
                        cb.check(timeout=5000)
                    except Exception:
                        # Workday's styled checkboxes hide the input behind
                        # overlay spans — check() can time out; click the
                        # associated label like a human (justfab 2026-07-19).
                        cb.evaluate(
                            "el => { const l = el.closest('label') || (el.id &&"
                            " document.querySelector(`label[for=\"${el.id}\"]`));"
                            " (l || el).click(); }")
                    self.pause()
            except Exception:
                continue

    # ---------- interactive in-run review (essay_policy: review) ----------
    def make_redraft(self, question: str):
        """Callback for the [r] review option: ask Claude for a different draft."""
        def _redraft(previous: str) -> str:
            d = tailor.draft_field_answer(
                self.cfg, self.profile, question,
                self.documents.get("jd_text", ""), self.posting.title,
                self.posting.company, avoid=previous or None,
            )
            return d.get("answer", "")
        return _redraft

    def review_answer(self, question: str, draft: str, redraft=None) -> str | None:
        """Terminal review of a held draft. Returns the final answer to use,
        or None to fall back to escalation (also when there's no terminal).
        `redraft`: optional callback(previous_draft) -> new draft, for [r]."""
        if not sys.stdin.isatty():
            return None
        while True:
            print("\n─── REVIEW NEEDED ─────────────────────────────")
            print(f"Q: {question}\n")
            print(f"Draft answer:\n{draft if draft else '(no draft yet — [r] asks Claude, [t]/[e] write your own)'}\n")
            opts = "[Enter] use draft   [e] edit in $EDITOR   [t] type replacement"
            if redraft:
                opts += "   [r] new Claude draft"
            opts += "   [s] skip → escalate"
            print(opts)
            try:
                choice = input("> ").strip().lower()
            except (KeyboardInterrupt, EOFError):
                return None
            if choice == "":
                if draft:
                    return draft
                continue  # nothing to accept yet
            if choice == "t":
                print("Type the answer (single line; use [e] for multi-line):")
                try:
                    text = input("> ").strip()
                except (KeyboardInterrupt, EOFError):
                    return None
                if text:
                    return text
                continue
            if choice == "e":
                editor = os.environ.get("EDITOR", "nano")
                fd, path = tempfile.mkstemp(suffix=".txt", text=True)
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        f.write(draft or "")
                    subprocess.call([editor, path])
                    text = Path(path).read_text(encoding="utf-8").strip()
                finally:
                    Path(path).unlink(missing_ok=True)
                if text:
                    return text
                continue
            if choice == "r" and redraft:
                print("… asking Claude for a different draft")
                try:
                    new = redraft(draft)
                except Exception as exc:
                    print(f"(redraft failed: {exc})")
                    continue
                if new:
                    draft = new
                else:
                    print("(no draft returned)")
                continue
            if choice == "s":
                return None
            # unrecognized input → show the prompt again

    def review_choice(self, question: str, options: list[str],
                      suggested: str | None = None) -> str | None:
        """Terminal review of a held choice question. Returns the chosen
        option text, or None to fall back to escalation."""
        if not sys.stdin.isatty():
            return None
        print("\n─── REVIEW NEEDED (choose one) ────────────────")
        print(f"Q: {question}\n")
        shown = options[:30]
        for i, opt in enumerate(shown, 1):
            marker = "  ← suggested" if suggested and opt == suggested else ""
            print(f"  [{i}] {opt}{marker}")
        if len(options) > len(shown):
            print(f"  … and {len(options) - len(shown)} more (skip → escalate to answer manually)")
        print("  [s] skip → escalate")
        try:
            choice = input("> ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            return None
        if choice.isdigit() and 1 <= int(choice) <= len(options):
            return options[int(choice) - 1]
        return None

    def approved_answer(self, question: str) -> str | None:
        """Look up a user-approved answer (from --answers) for this question."""
        approved = self.documents.get("approved_answers") or {}
        q = self._norm(question)
        for stored_q, answer in approved.items():
            s = self._norm(stored_q)
            if s == q or s in q or q in s:
                return answer
        return None

    def identity_value(self, label_text: str) -> str | None:
        """standard_value guarded for identity pre-fill: long labels are
        QUESTIONS ('...team specific locations...'), not field names, and must
        go through the question pipeline instead of naive pattern matching."""
        if len(label_text.strip()) > 60:
            return None
        return self.standard_value(label_text)

    def answer_custom_question(self, question: str) -> dict:
        """Answer a form question: user-approved answers first (never held),
        then standing answers (relocation), then a Claude draft honoring the
        essay/confidence policy."""
        approved = self.approved_answer(question)
        if approved:
            return {"answer": approved, "confidence": "high",
                    "is_essay": False, "hold": False, "source": "approved"}
        # Standing answer: free-text relocation questions (user directive)
        if re.search(r"relocat", question.lower()):
            reloc = self.profile["standard_answers"].get("relocation_answer")
            if reloc:
                return {"answer": reloc, "confidence": "high",
                        "is_essay": False, "hold": False, "source": "profile"}
        # Standing answer: employee-referral name fields are left BLANK (user
        # 2026-07-21: no contacts at other companies) — never drafted by Claude
        # (which would invent a name) and never held.
        if _EMPLOYEE_REFERRAL_RE.search(question):
            return {"answer": "", "confidence": "high",
                    "is_essay": False, "hold": False, "source": "profile"}
        # Standing answer: free-text "how did you hear about us" (user
        # directive 2026-07-14 — choice variants already resolve via
        # choice_value; this covers the text-input variant).
        if re.search(r"how did you (hear|find)|hear about", question.lower()):
            heard = self.profile["standard_answers"].get("how_did_you_hear")
            if heard:
                return {"answer": heard, "confidence": "high",
                        "is_essay": False, "hold": False, "source": "profile"}
        draft = tailor.draft_field_answer(
            self.cfg, self.profile, question,
            self.documents["jd_text"], self.posting.title, self.posting.company,
        )
        policy = self.auto.get("essay_policy", "hold")
        hold = (draft.get("is_essay") and policy in ("hold", "review")) \
            or draft.get("confidence") == "low"
        if hold and policy == "review":
            reviewed = self.review_answer(question, draft.get("answer", ""),
                                          redraft=self.make_redraft(question))
            if reviewed is not None:
                return {"answer": reviewed, "confidence": "high",
                        "is_essay": draft.get("is_essay", False),
                        "hold": False, "source": "reviewed"}
        draft["hold"] = hold
        return draft

    def escalate_now(self, page, reason: str, extra: dict | None = None) -> RunResult:
        shot = None
        html = None
        try:
            shot = page.screenshot(full_page=True)
            html = page.content()
        except Exception:
            pass
        case = escalate(
            self.cfg["paths"]["pending_dir"], reason=reason, url=self.posting.final_url,
            company=self.posting.company, title=self.posting.title,
            screenshot_bytes=shot, page_html=html, extra=extra,
        )
        notify(self.cfg, f"Escalated ({reason}): {self.posting.title} @ {self.posting.company} → {case}")
        return RunResult(status="escalated", reason=reason, details={"case": str(case)})

    # ---------- to implement per ATS ----------
    def apply(self, page) -> RunResult:
        raise NotImplementedError
