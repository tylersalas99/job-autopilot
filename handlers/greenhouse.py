"""Greenhouse handler.

Greenhouse application forms are single-page: identity fields, resume upload,
optional cover letter, custom questions, EEO section, submit. Field ids are
stable (#first_name, #last_name, #email, #phone) on classic boards; the newer
job-boards.greenhouse.io uses labeled inputs, which the label-driven fallback
below covers.
"""
from __future__ import annotations

import re

from handlers.base import BaseHandler, RunResult


class GreenhouseHandler(BaseHandler):
    ats_name = "greenhouse"

    ID_FIELDS = {
        "#first_name": "first_name",
        "#last_name": "last_name",
        "#email": "email",
        "#phone": "phone",
    }

    def apply(self, page) -> RunResult:
        page.goto(self.posting.final_url, wait_until="domcontentloaded")
        self.pause()

        if self.detect_captcha(page):
            return self.escalate_now(page, "CAPTCHA present on load")

        # Some postings put the form behind an "Apply" button
        for sel in ("a[href*='#app']", "button:has-text('Apply')", "a:has-text('Apply')"):
            btn = page.query_selector(sel)
            if btn and not page.query_selector("#first_name, input[name*='first']"):
                btn.click()
                self.pause()
                break

        # --- identity fields (classic ids, then label fallback) ---
        ident = self.profile["identity"]
        for selector, key in self.ID_FIELDS.items():
            self.fill_if_present(page, selector, ident[key])
        self._fill_labeled_inputs(page)

        # --- resume upload ---
        resume_input = page.query_selector(
            "input[type='file'][name*='resume'], input[type='file'][id*='resume'], "
            "#resume, input[type='file']"
        )
        if not resume_input:
            return self.escalate_now(page, "No resume upload field found")
        resume_input.set_input_files(str(self.documents["resume"]))
        self.pause()
        page.wait_for_timeout(2500)  # let Greenhouse's resume parse settle

        cl = page.query_selector(
            "input[type='file'][name*='cover'], input[type='file'][id*='cover']")
        if cl and self.documents.get("cover_letter"):
            cl.set_input_files(str(self.documents["cover_letter"]))
            self.pause()

        # --- Location (City): required react-select typeahead on job-boards
        # forms. Type the city and pick the suggested match ("El Paso" →
        # "El Paso, Texas, United States").
        #
        # State/country-qualified candidates ONLY — never a bare city name.
        # On the Sony run (2026-07-21) the bare "El Paso" candidate matched
        # BOTH "El Paso, Texas, United States" and "El Paso, Cesar, Colombia";
        # match_option's shortest-superset rule then picked the shorter
        # foreign option, so the form was submitted with a Colombian city.
        # Including the state ("Texas") in the matched text makes the US
        # option the only substring hit, and "TX" won't token-match "Texas".
        self._pick_react_option(page, "candidate-location",
                                self._location_candidates(ident),
                                type_first=True, fuzzy=True)

        # --- education section (job-boards UI), from profile['education'] ---
        self._fill_education(page)

        # --- custom questions, dropdowns (incl. EEO), radio groups ---
        held = self._handle_custom_questions(page)
        held += self.handle_native_selects(page)
        held += self.handle_react_selects(page)
        held += self.handle_radio_groups(page)
        held += self.handle_checkbox_groups(page)
        if held:
            return self.escalate_now(
                page, "Essay/low-confidence questions held for your review",
                extra={"held_questions": held},
            )

        # --- unanswered required fields? resolve in-terminal, else escalate ---
        unresolved = []
        for el, label in self._missing_required_fields(page):
            if self.auto.get("essay_policy") == "review":
                try:
                    tag = el.evaluate("el => el.tagName")
                except Exception:
                    tag = ""
                if tag in ("INPUT", "TEXTAREA") and el.get_attribute("role") != "combobox":
                    answer = self.review_answer(label, "", redraft=self.make_redraft(label))
                    if answer:
                        el.fill(answer)
                        self.pause()
                        continue
            unresolved.append(label)
        if unresolved:
            return self.escalate_now(page, f"Unmapped required fields: {unresolved[:5]}")

        if self.detect_captcha(page):
            return self.escalate_now(page, "CAPTCHA before submit")

        # --- submit ---
        if self.auto.get("supervised_mode", True):
            print("\n⏸  SUPERVISED MODE: review the browser window, then press Enter to submit "
                  "(or Ctrl+C to abort)...")
            try:
                input()
            except KeyboardInterrupt:
                return RunResult(status="held", reason="Aborted at supervised-mode gate")

        submit = page.query_selector(
            "#submit_app, button[type='submit'], input[type='submit']"
        )
        if not submit:
            return self.escalate_now(page, "Submit button not found")
        submit.click()
        page.wait_for_timeout(4000)

        return self._verify(page)

    # ---------- helpers ----------
    @staticmethod
    def degree_candidates(degree: str) -> list[str]:
        """Option texts to try for a profile degree string like
        'B.S. in Computer Science' — Greenhouse degree lists vary
        ('Bachelor of Science' vs \"Bachelor's Degree\")."""
        d = degree.lower()
        table = [
            (r"\bb\.?\s*s\b|bachelor.*science",
             ["Bachelor of Science", "Bachelor's Degree", "Bachelor's", "Bachelors"]),
            (r"\bb\.?\s*a\b|bachelor.*arts",
             ["Bachelor of Arts", "Bachelor's Degree", "Bachelor's", "Bachelors"]),
            (r"\bm\.?\s*s\b|master.*science",
             ["Master of Science", "Master's Degree", "Master's", "Masters"]),
            (r"\bm\.?\s*a\b|master.*arts",
             ["Master of Arts", "Master's Degree", "Master's", "Masters"]),
            (r"\bmba\b", ["MBA", "Master of Business Administration"]),
            (r"\bph\.?\s*d\b|doctor", ["Ph.D.", "PhD", "Doctorate"]),
            (r"associate", ["Associate's Degree", "Associate's", "Associates"]),
        ]
        for pattern, candidates in table:
            if re.search(pattern, d):
                return candidates
        return [degree] if degree else []

    def _fill_education(self, page) -> None:
        """Fill the first education row (school/degree/discipline/end year)
        from profile['education'][0]. Best-effort: anything that doesn't
        match cleanly is left alone — the react-select pass or the
        required-fields check picks it up, and nothing is ever guessed."""
        edu = (self.profile.get("education") or [None])[0]
        if not edu:
            return
        degree = str(edu.get("degree") or "")
        # School list is an async typeahead — type the name first. Fuzzy:
        # option text often reformats the name. school_aliases carries known
        # ATS option-text variants ("University of Texas - El Paso") tried
        # as separate search queries when the canonical name finds nothing.
        school_candidates = [edu.get("school")] + list(edu.get("school_aliases") or [])
        self._pick_react_option(page, "school--0", school_candidates,
                                type_first=True, fuzzy=True)
        self._pick_react_option(page, "degree--0", self.degree_candidates(degree))
        # "B.S. in Computer Science" → discipline "Computer Science"
        discipline = re.sub(r"^.*?\bin\b\s+", "", degree).strip()
        self._pick_react_option(page, "discipline--0", [discipline])
        grad_year = re.match(r"(\d{4})", str(edu.get("graduated") or ""))
        for input_id, key in (("start-year--0", "started"), ("end-year--0", "graduated")):
            m = re.match(r"(\d{4})", str(edu.get(key) or ""))
            inp = page.query_selector(f"[id='{input_id}']")
            if m and inp and not inp.input_value():
                inp.fill(m.group(1))
                self.pause()

    @staticmethod
    def _open_options(page, input_id: str) -> list[tuple]:
        """(element, text) pairs for the currently open react-select menu."""
        opts = page.query_selector_all(f"[id^='react-select-{input_id}-option']")
        pairs = [(o, (o.inner_text() or "").strip()) for o in opts if o.is_visible()]
        return [(o, t) for o, t in pairs if t]

    @staticmethod
    def _location_candidates(ident: dict) -> list[str]:
        """State/country-qualified location strings for the react-select
        location typeahead, most-specific first. A bare city ("El Paso")
        must never be a candidate: it substring-matches foreign cities of
        the same name and match_option's shortest-superset rule then picks
        the wrong one (Sony run 2026-07-21 → "El Paso, Cesar, Colombia").
        Every candidate here carries the state name, so the US option is
        the only possible match. The city/state_full/country fields are the
        typed queries too, and "El Paso, Texas" reliably surfaces the US
        city in Greenhouse's location search."""
        city = (ident.get("city") or "").strip()
        state_full = (ident.get("state_full") or "").strip()
        country = (ident.get("country") or "").strip()
        loc = (ident.get("location") or "").strip()  # e.g. "El Paso, TX"
        out: list[str] = []
        for c in (
            f"{city}, {state_full}, {country}" if city and state_full and country else "",
            f"{city}, {state_full}" if city and state_full else "",
            loc if "," in loc else "",  # keep only if it carries a region tail
        ):
            if c and c not in out:
                out.append(c)
        return out

    @classmethod
    def _typeahead_queries(cls, candidates: list[str]) -> list[str]:
        """Search strings to type, in order: each candidate verbatim, then a
        distinctive-tail query per candidate (last two significant tokens —
        'el paso' for 'University of Texas - El Paso'). Short queries surface
        the entry however the backend formats its name; the fuzzy match
        against the full candidate then picks the right suggestion."""
        queries: list[str] = []

        def add(q: str) -> None:
            if q and q.lower() not in (x.lower() for x in queries):
                queries.append(q)

        for c in candidates:
            add(c)
        for c in candidates:
            toks = [w for w in cls._norm(c).split() if w not in cls._STOP_TOKENS]
            if len(toks) >= 3:
                add(" ".join(toks[-2:]))
        return queries

    def _poll_match(self, page, input_id: str, candidates: list[str],
                    fuzzy: bool, timeout_ms: int = 6000):
        """Poll the open menu until an option matches a candidate (async
        searches race a fixed wait — the old 1200ms lost). Returns the option
        element or None. A visible 'No options' notice (while not loading)
        ends the wait early; a stale/default list simply fails to match and
        the next tick sees the refreshed one."""
        waited = 0
        while waited < timeout_ms:
            page.wait_for_timeout(300)
            waited += 300
            pairs = self._open_options(page, input_id)
            options = [t for _, t in pairs]
            for desired in candidates:
                idx = self.match_option(desired, options)
                if idx is None and fuzzy:
                    idx = self.fuzzy_index(desired, options)
                if idx is not None:
                    return pairs[idx][0]
            if not pairs:
                loading = page.query_selector(".select__menu-notice--loading")
                empty = page.query_selector(".select__menu-notice--no-options")
                if empty and empty.is_visible() and not (loading and loading.is_visible()):
                    return None
        return None

    def _pick_react_option(self, page, input_id: str, candidates: list,
                           type_first: bool = False, fuzzy: bool = False) -> bool:
        """Open react-select `input_id` and pick the first candidate that
        matches an option. Returns True when something was selected."""
        candidates = [str(c) for c in candidates if c]
        if not candidates:
            return False
        inp = page.query_selector(f"[id='{input_id}']")
        if not inp:
            return False
        try:
            filled = inp.evaluate(
                "el => { const c = el.closest('.select__container, .select-shell, .select');"
                " return !!(c && c.querySelector('.select__single-value, .select__multi-value')); }")
            if filled:
                return True
            if type_first:
                # Async typeaheads (school, location) search on what's typed —
                # and the backend's canonical name may not match the profile's
                # ("The University of Texas at El Paso" returns nothing when
                # the DB entry is "University of Texas - El Paso"). Type each
                # query in turn — candidates, then distinctive-tail fallbacks —
                # and match EVERY candidate against whatever each search
                # returns. Keystroke typing (not fill) drives the debounced
                # search the way a human would.
                for typed in self._typeahead_queries(candidates):
                    inp.click()
                    inp.fill("")
                    # ElementHandle.type() — the Locator-only sequential-press
                    # method doesn't exist on ElementHandles and its
                    # AttributeError gets swallowed by the except below (this
                    # silently killed location AND school on 2026-07-14).
                    inp.type(typed, delay=40)
                    option = self._poll_match(page, input_id, candidates, fuzzy)
                    if option is not None:
                        option.click()
                        self.pause()
                        return True
                inp.fill("")
                page.keyboard.press("Escape")
                return False
            inp.click()
            page.wait_for_timeout(500)
            pairs = self._open_options(page, input_id)
            options = [t for _, t in pairs]
            for desired in candidates:
                idx = self.match_option(desired, options)
                if idx is None and fuzzy:
                    idx = self.fuzzy_index(desired, options)
                if idx is not None:
                    pairs[idx][0].click()
                    self.pause()
                    return True
            page.keyboard.press("Escape")
        except Exception:
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
        return False

    def _fill_labeled_inputs(self, page) -> None:
        for label_el in page.query_selector_all("label"):
            try:
                text = (label_el.inner_text() or "").strip()
            except Exception:
                continue
            if not text:
                continue
            value = self.identity_value(text)
            if value is None:
                continue
            target = label_el.get_attribute("for")
            # [id='...'] not #id: job-boards uses NUMERIC ids ("326") on the
            # demographic questions, and '#326' is invalid CSS (crashes).
            inp = (page.query_selector(f"[id='{target}']") if target
                   else label_el.query_selector("input"))
            if not inp:
                continue
            if inp.get_attribute("role") == "combobox":
                continue  # react-select inner input — never type into it here
            # Identity pre-fill only touches short text inputs — textareas are
            # custom questions and belong to the question pipeline.
            if inp.evaluate("el => el.tagName") == "TEXTAREA":
                continue
            itype = inp.get_attribute("type")
            if itype in ("file", "checkbox", "radio"):
                continue
            # fill() of text into input[type=number] throws immediately —
            # only numeric values may go into numeric inputs.
            if itype == "number" and not str(value).strip().isdigit():
                continue
            if not inp.input_value():
                inp.fill(value)
                self.pause()

    def _handle_custom_questions(self, page) -> list[dict]:
        """Answer custom questions via Claude; return list of held items.

        Covers textareas AND single-line text inputs whose label is
        question-length (>60 chars) — Greenhouse renders short-answer custom
        questions as <input class="input__single-line">, not <textarea>.
        """
        held = []
        for field in page.query_selector_all(
            "textarea, input[type='text'], input:not([type])"
        ):
            try:
                if field.get_attribute("role") == "combobox":
                    continue  # react-select's inner input — handled separately
                if field.input_value():
                    continue
                label = self._label_for(page, field)
                if not label:
                    continue
                is_textarea = field.evaluate("el => el.tagName") == "TEXTAREA"
                if not is_textarea and len(label) <= 60:
                    continue  # short-label input = identity field, not a question
                draft = self.answer_custom_question(label)
                if draft["hold"]:
                    held.append({"question": label, "draft_answer": draft.get("answer", "")})
                else:
                    field.fill(draft["answer"])
                    self.pause()
            except Exception:
                continue
        return held

    def _label_for(self, page, el) -> str:
        el_id = el.get_attribute("id")
        if el_id:
            lab = page.query_selector(f"label[for='{el_id}']")
            if lab:
                return (lab.inner_text() or "").strip()
        return (el.get_attribute("aria-label") or el.get_attribute("placeholder") or "").strip()

    def _missing_required_fields(self, page) -> list[tuple]:
        """Return (element, label) pairs for required-but-empty fields."""
        missing = []
        for el in page.query_selector_all(
            "input[required], textarea[required], select[required], "
            "input[aria-required='true'], textarea[aria-required='true'], "
            "select[aria-required='true']"
        ):
            if el.get_attribute("type") in ("file", "checkbox", "radio", "hidden"):
                continue
            if el.get_attribute("role") == "combobox":
                # react-select: the input stays empty; the chosen value lives in
                # a .select__single-value node inside the container
                filled = el.evaluate(
                    "el => { const c = el.closest('.select__container, .select-shell, .select');"
                    " return !!(c && c.querySelector('.select__single-value, .select__multi-value')); }")
                if filled:
                    continue
                missing.append((el, self.field_label(page, el)
                                or el.get_attribute("name") or el.get_attribute("id") or "?"))
                continue
            if not el.input_value():
                missing.append((el, self._label_for(page, el) or el.get_attribute("name")
                                or el.get_attribute("id") or "?"))
        return missing

    def _verify(self, page) -> RunResult:
        content = page.content().lower()
        success_markers = ["thank you for applying", "application has been submitted",
                           "application submitted", "we have received your application",
                           "thanks for applying"]
        if any(m in content for m in success_markers) or "confirmation" in page.url:
            return RunResult(status="submitted", reason="Confirmation detected")
        error = page.query_selector(".error, [class*='error']:not([class*='hidden'])")
        if error:
            return self.escalate_now(page, f"Submission error: {(error.inner_text() or '')[:200]}")
        return self.escalate_now(page, "Submission unverified — no confirmation marker found")
