"""Ashby handler (beta).

Ashby (jobs.ashbyhq.com) renders a React SPA — field names are generated, so
this handler is label-driven: it reads each field's label text and maps it via
the profile store. Anything it can't confidently map escalates. Expect more
escalations here than on Greenhouse/Lever until it's tuned against real runs.

Widget inventory (captured live 2026-07-14 against OpenAI/Ashby/ElevenLabs
boards — Ashby renders forms from typed field definitions, so rendering is
consistent across companies):

- ``String``/``Email``/``Phone``/``LongText`` → plain inputs/textareas
  (label-driven loop below).
- ``Location`` → async typeahead combobox: ``input[role='combobox']
  [aria-haspopup='listbox']`` with placeholder "Start typing...". Options
  appear only AFTER typing, in a ``[role='listbox']`` PORTALED TO <body>
  (class ``_floatingContainer_*``) — never inside the field entry, so
  options are queried page-wide (safe: one menu open at a time). The
  chosen value is committed into ``input.value`` (unlike Greenhouse's
  react-select), so a non-empty input means answered.
- ``ValueSelect``/EEO fields with SHORT option lists → native radio
  inputs (handled by handle_radio_groups). Long lists render as the same
  combobox component with a static option list that opens on click.
- ``Boolean`` → Yes/No BUTTON pair (NO type attribute; the DOM-property
  default makes them look like type=submit in devtools, but the CSS
  attribute selector button[type='submit'] does NOT match them) inside a
  ``[class*='_yesno']`` container, plus a hidden mirror checkbox. The
  chosen button gains an ``_active`` class; checkbox.checked mirrors the
  VALUE (false for "No"), so it must never be used as the answered signal.
- ``Date`` → text input with placeholder "Pick date..." backed by a
  calendar widget. fill() puts text React ignores — skipped in the fill
  loop, left to the required-field check (known gap).
- ``MultiValueSelect`` with one option → consent checkbox (consent
  handler's territory).
"""
from __future__ import annotations

import re

from handlers.base import BaseHandler, RunResult

_LOCATIONISH_RE = re.compile(
    r"located|location|country.*(work|reside|live|based)|where are you", re.I)


class AshbyHandler(BaseHandler):
    ats_name = "ashby"

    def apply(self, page) -> RunResult:
        url = self.posting.final_url
        if "/application" not in url:
            url = url.rstrip("/") + "/application"
        page.goto(url, wait_until="networkidle")
        self.pause()

        if self.detect_captcha(page):
            return self.escalate_now(page, "CAPTCHA present on load")

        # Resume first: Ashby autofills several fields from the parsed resume
        file_input = page.query_selector("input[type='file']")
        if not file_input:
            return self.escalate_now(page, "No resume upload field found")
        file_input.set_input_files(str(self.documents["resume"]))
        page.wait_for_timeout(4000)  # let autofill-from-resume settle

        held = []
        for container in page.query_selector_all("[class*='_fieldEntry'], .ashby-application-form-field-entry, label"):
            try:
                # Full text, whitespace-normalized: long screening questions
                # (coding prompts, "share a Loom video" asks) span multiple
                # lines and blow past any single-line or short-label cut.
                # identity_value() already refuses labels >60 chars, so long
                # text safely routes to the question pipeline instead.
                label_text = " ".join((container.inner_text() or "").split())
            except Exception:
                continue
            if not label_text or len(label_text) > 600:
                continue
            field = container.query_selector("input:not([type='file']), textarea") \
                or self._sibling_field(container)
            if not field or field.get_attribute("type") in ("checkbox", "radio", "hidden"):
                continue
            if field.get_attribute("role") == "combobox":
                continue  # Ashby typeahead — handle_comboboxes' territory; never fill()
            if (field.get_attribute("placeholder") or "").lower().startswith("pick date"):
                continue  # calendar widget — fill() text is ignored by React (known gap)
            try:
                if field.input_value():
                    continue  # autofilled from resume — leave it
            except Exception:
                continue

            value = self.identity_value(label_text)
            if value is not None:
                field.fill(value)
                self.pause()
                continue

            # Unknown field → Claude drafts, honoring hold policy
            draft = self.answer_custom_question(label_text)
            if draft["hold"]:
                held.append({"question": label_text, "draft_answer": draft.get("answer", "")})
            else:
                field.fill(draft["answer"])
                self.pause()

        # React comboboxes (Location + long ValueSelects) and Boolean
        # yes/no button pairs; then native selects/radios (EEO sections and
        # short ValueSelects render as real radio inputs).
        held += self.handle_comboboxes(page)
        held += self.handle_yesno_buttons(page)
        held += self.handle_native_selects(page)
        held += self.handle_radio_groups(page)
        held += self.handle_checkbox_groups(page)
        self.handle_consent_checkboxes(page)
        if held:
            return self.escalate_now(
                page, "Essay/low-confidence questions held for your review",
                extra={"held_questions": held},
            )

        # Escalate on any visibly required-but-empty field rather than guess
        empties = []
        for el in page.query_selector_all(
                "input[aria-required='true'], textarea[aria-required='true'], "
                "input[required], textarea[required]"):
            try:
                if el.get_attribute("type") not in ("file", "checkbox", "radio") and not el.input_value():
                    empties.append(el.get_attribute("aria-label") or el.get_attribute("id") or "?")
            except Exception:
                continue
        # Ashby widgets carry no aria-required/required attribute — the
        # requiredness lives in a `_required_` class on the entry's label.
        # Catch unanswered required comboboxes and yes/no groups here so a
        # missed widget escalates instead of failing Ashby's client-side
        # validation after submit ("Submission unverified").
        for inp in page.query_selector_all("input[role='combobox']"):
            try:
                if inp.is_visible() and not inp.input_value() \
                        and self._entry_is_required(inp):
                    empties.append(self._entry_label(inp) or "combobox?")
            except Exception:
                continue
        for cont in page.query_selector_all("[class*='_yesno']"):
            try:
                if not cont.query_selector("button[class*='_active']") \
                        and self._entry_is_required(cont):
                    empties.append(self._entry_label(cont) or "yes/no?")
            except Exception:
                continue
        if empties:
            return self.escalate_now(page, f"Unmapped required fields: {empties[:5]}")

        if self.detect_captcha(page):
            return self.escalate_now(page, "CAPTCHA before submit")

        if self.auto.get("supervised_mode", True):
            print("\n⏸  SUPERVISED MODE: review the browser window, then press Enter to submit "
                  "(or Ctrl+C to abort)...")
            try:
                input()
            except KeyboardInterrupt:
                return RunResult(status="held", reason="Aborted at supervised-mode gate")

        submit = self._visible(
            page,
            "button[type='submit'], button:has-text('Submit Application'), button:has-text('Submit')"
        )
        if not submit:
            return self.escalate_now(page, "No visible submit button found")
        submit.click()
        page.wait_for_timeout(4000)

        content = page.content().lower()
        if "success" in page.url or "thank you" in content \
                or "application submitted" in content \
                or "successfully submitted" in content \
                or page.query_selector("[class*='success-container']"):
            return RunResult(status="submitted", reason="Confirmation detected")
        return self.escalate_now(page, "Submission unverified — no confirmation marker found")

    # ---------- Ashby react widgets ----------
    @staticmethod
    def _entry_label(el) -> str:
        """Question text for a control: its field entry's title label.
        (The combobox input has NO id — the label's `for` points at the
        data-field-path, so label[for] lookup can't work here.)"""
        try:
            text = el.evaluate(
                "el => { const f = el.closest(\"[class*='_fieldEntry']\");"
                " const l = f && f.querySelector('label');"
                " return l ? l.innerText : ''; }")
            return " ".join((text or "").split())
        except Exception:
            return ""

    @staticmethod
    def _entry_is_required(el) -> bool:
        try:
            return bool(el.evaluate(
                "el => { const f = el.closest(\"[class*='_fieldEntry']\");"
                " const l = f && f.querySelector('label');"
                " return !!(l && /_required_/.test(l.className)); }"))
        except Exception:
            return False

    @staticmethod
    def _open_menu_options(page) -> list[tuple]:
        """(element, text) pairs for the currently open combobox menu.
        Ashby portals the [role='listbox'] to <body>, so options can only
        be found page-wide — safe because a single menu is open at a time."""
        opts = page.query_selector_all("[role='listbox'] [role='option']")
        pairs = [(o, " ".join((o.inner_text() or "").split())) for o in opts
                 if o.is_visible()]
        return [(o, t) for o, t in pairs if t]

    def _poll_combo_match(self, page, candidates: list[str], fuzzy: bool,
                          timeout_ms: int = 6000):
        """Poll the open menu until an option matches a candidate. Async
        location searches race any fixed wait (the Greenhouse school picker
        lost a run to that) — poll instead. Returns the option element or
        None."""
        waited = 0
        while waited < timeout_ms:
            page.wait_for_timeout(300)
            waited += 300
            pairs = self._open_menu_options(page)
            options = [t for _, t in pairs]
            for desired in candidates:
                idx = self.match_option(desired, options)
                if idx is None and fuzzy:
                    idx = self.fuzzy_index(desired, options)
                if idx is not None:
                    return pairs[idx][0]
            # "No results"-style notice while not searching → stop early
            if not pairs:
                lb = page.query_selector("[role='listbox']")
                if lb and lb.is_visible() and re.search(
                        r"no (results|options|matches)", lb.inner_text() or "", re.I):
                    return None
        return None

    def handle_comboboxes(self, page) -> list[dict]:
        """Fill Ashby react comboboxes. Two flavors share one component:

        - static (long ValueSelects): clicking the input opens the full
          option list → resolve via answer_choice (profile-first).
        - async typeahead (Location): nothing opens until you type → type
          the profile city/country keystroke-by-keystroke and poll for a
          fuzzy match, exactly like the Greenhouse location picker.

        Anything unresolved is HELD, never guessed. Selection is verified
        by the committed input.value; on any failure the menu is Escaped
        so a stuck-open portal can't shadow later widgets."""
        held = []
        for inp in page.query_selector_all("input[role='combobox']"):
            try:
                if not inp.is_visible() or inp.input_value():
                    continue  # hidden, or already answered (value commits to input)
                question = self._entry_label(inp)
                if not question:
                    continue
                inp.click()
                pairs = []
                waited = 0
                while waited < 1500 and not pairs:  # static lists open on click
                    page.wait_for_timeout(300)
                    waited += 300
                    pairs = self._open_menu_options(page)
                if pairs:
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
                    continue
                # No options on click → async typeahead. Only location-style
                # questions have a deterministic profile answer worth typing;
                # anything else is held for review.
                if not _LOCATIONISH_RE.search(question):
                    held.append({"question": question, "options": [],
                                 "draft_answer": ""})
                    page.keyboard.press("Escape")
                    continue
                ident = self.profile["identity"]
                candidates = [str(c) for c in (
                    ident.get("city"), ident.get("location"), ident.get("country")) if c]
                # Country-style asks ("Which country do you intend to work
                # from?") surface country options — try that query first.
                queries = [ident.get("country"), ident.get("city")] \
                    if re.search(r"\bcountry\b", question, re.I) \
                    else [ident.get("city"), ident.get("country")]
                picked = False
                for typed in [str(q) for q in queries if q]:
                    inp.click()
                    inp.fill("")
                    # keystroke typing drives the debounced search; fill()
                    # doesn't (see the ElementHandle-not-Locator rule)
                    inp.type(typed, delay=40)
                    option = self._poll_combo_match(page, candidates, fuzzy=True)
                    if option is not None:
                        option.click()
                        self.pause()
                        picked = True
                        break
                if not picked:
                    inp.fill("")
                    page.keyboard.press("Escape")
                    held.append({"question": question, "options": [],
                                 "draft_answer": ""})
                elif not inp.input_value():
                    # option click didn't commit — treat as unanswered
                    held.append({"question": question, "options": [],
                                 "draft_answer": ""})
            except Exception:
                try:
                    page.keyboard.press("Escape")
                except Exception:
                    pass
                continue
        return held

    def handle_yesno_buttons(self, page) -> list[dict]:
        """Answer Ashby Boolean questions (Yes/No button pairs). Resolution
        is profile-first via answer_choice (work auth → Yes, sponsorship →
        No, ...); unknown questions are held. The buttons have NO type
        attribute, so the submit-button selector never matches them, and
        the hidden mirror checkbox must stay untouched (checked == the
        VALUE, not answered-ness — the `_active` class is the state)."""
        held = []
        for cont in page.query_selector_all("[class*='_yesno']"):
            try:
                if cont.query_selector("button[class*='_active']"):
                    continue  # already answered
                pairs = [(b, " ".join((b.inner_text() or "").split()))
                         for b in cont.query_selector_all("button") if b.is_visible()]
                pairs = [(b, t) for b, t in pairs if t]
                if len(pairs) < 2:
                    continue
                question = self._entry_label(cont)
                options = [t for _, t in pairs]
                if not question or question in options:
                    held.append({"question": question or "?", "options": options,
                                 "draft_answer": ""})
                    continue
                res = self.answer_choice(question, options)
                if res["hold"] or res["value"] is None:
                    held.append({"question": question, "options": options,
                                 "draft_answer": res["value"] or ""})
                    continue
                for b, t in pairs:
                    if t == res["value"]:
                        b.click()
                        self.pause()
                        break
            except Exception:
                continue
        return held

    def _sibling_field(self, label_el):
        try:
            handle = label_el.evaluate_handle(
                "el => el.nextElementSibling && el.nextElementSibling.matches('input,textarea') "
                "? el.nextElementSibling : null"
            )
            return handle.as_element()
        except Exception:
            return None
