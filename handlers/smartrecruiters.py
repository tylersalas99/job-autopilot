"""SmartRecruiters handler (beta — not yet live-proven; supervise first runs).

DOM contract (captured live 2026-07-19 against Visa's "Easy Apply" form at
jobs.smartrecruiters.com/oneclick-ui — SmartRecruiters renders every
company's apply form from the same Angular SPA, so structure is consistent):

- **Apply URL is deterministic** — never click "I'm interested": the public
  posting API's ``uuid`` field IS the publication UUID, and the form lives at
  ``jobs.smartrecruiters.com/oneclick-ui/company/<companyIdentifier>/``
  ``publication/<uuid>?dcr_ci=<companyIdentifier>``. Intake rewrites
  final_url to this (``_enrich_smartrecruiters``); if that failed we land on
  the job page and click the "I'm interested" control as a fallback.
- **Everything is shadow DOM**: fields are ``spl-*`` web components (open
  shadow roots) with the real ``<input>``/``<textarea>`` inside. Playwright's
  CSS engine PIERCES open shadow roots, so plain ``query_selector`` works —
  but ``el.closest(...)``/``document.getElementsByName`` inside ``evaluate()``
  do NOT cross shadow boundaries. Labels sit in the same shadow root with
  proper ``for`` attributes, so base ``field_label``'s label[for] path works.
- **Stable input ids** (step 1, "Personal information"): first-name-input,
  last-name-input, email-input, confirm-email-input, linkedin-input,
  facebook-input, twitter-input, website-input, hiring-manager-message-input
  (textarea), file-input (resume, inside spl-dropzone). Phone is
  ``input[type=tel]`` (aria-label "Phone number") inside spl-phone-field with
  a country dropdown. City is an spl-autocomplete typeahead whose input id is
  GENERATED (spl-form-element_N) — reach it via its label text instead.
  Experience/Education are optional "Add" panels — left alone (the resume
  carries that content).
- **Two file inputs exist**: an avatar uploader (aria-label "Upload profile
  image") and the resume dropzone (#file-input). Never upload the resume to
  a bare ``input[type=file]`` query — it grabs the avatar input first.
- **Required marking**: label text ends with ``*`` ("First name*"); Angular
  mirrors validity as ``ng-invalid`` class on the spl-* HOST element (not
  the inner input). After filling, any visible ng-invalid host means a
  required field is empty or malformed — escalate with its label.
- **Multi-step wizard**: step 1 profile → "Next" (an ``spl-button``; its
  clickable <button> is in shadow, clicking the host works) → screening
  questions/consents → final submit. Step-2 widget DOM is NOT yet captured
  live (Visa's form couldn't be advanced without entering data) — the
  generic base passes + ng-invalid check cover it; expect escalations there
  until a supervised run pins it down.
- **Do NOT match submit buttons by "Apply" substring**: the form carries
  "Apply With Indeed" / "Apply with SEEK" / "Apply with LinkedIn" partner
  buttons. ``_submitish`` requires an exact submit/apply phrase and excludes
  partner names.
- **DataDome bot protection** is loaded (api-js.datadome.co). Its challenge
  renders as a captcha-delivery.com iframe — detected in detect_captcha
  below, on top of the base recaptcha/hcaptcha checks. Human pacing matters
  here; never try to solve it (hard rule).
- **OneTrust cookie banner** overlays the form on first visit —
  #onetrust-reject-all-handler is clicked if visible (privacy-preserving).
"""
from __future__ import annotations

import re

from handlers.base import BaseHandler, RunResult

# Partner one-click buttons that contain "Apply" but are NOT the submit
_PARTNER_RE = re.compile(r"indeed|seek|linkedin|pitchyou", re.I)
_SUBMIT_TEXT_RE = re.compile(
    r"^(submit( application)?|apply( now)?|send application)$", re.I)

# The "City"/place-of-residence typeahead, matched by label text
_CITYISH_RE = re.compile(r"\bcity\b|place of residence|location", re.I)


class SmartRecruitersHandler(BaseHandler):
    ats_name = "smartrecruiters"

    MAX_STEPS = 6

    # ---------- captcha (base + DataDome) ----------
    def detect_captcha(self, page) -> bool:
        if super().detect_captcha(page):
            return True
        for sel in ("iframe[src*='captcha-delivery']", "iframe[src*='datadome']"):
            for el in page.query_selector_all(sel):
                try:
                    if el.is_visible():
                        box = el.bounding_box()
                        if box and box["width"] > 50 and box["height"] > 50:
                            return True
                except Exception:
                    continue
        return False

    # ---------- main flow ----------
    def apply(self, page) -> RunResult:
        page.goto(self.posting.final_url, wait_until="networkidle")
        self.pause()
        self._dismiss_cookie_banner(page)

        # Intake normally rewrites final_url to the oneclick-ui form; if we
        # landed on the job page instead, follow "I'm interested".
        if "/oneclick-ui/" not in page.url:
            trigger = self._visible(
                page, "a[href*='oneclick-ui'], button:has-text(\"I'm interested\")")
            if not trigger:
                return self.escalate_now(page, "Apply form not reached — no "
                                         "oneclick-ui URL and no \"I'm interested\" control")
            trigger.click()
            page.wait_for_load_state("networkidle")
            self.pause()
            self._dismiss_cookie_banner(page)

        if self.detect_captcha(page):
            return self.escalate_now(page, "CAPTCHA present on load")

        # Resume first (dropzone-scoped: a bare input[type=file] query grabs
        # the avatar uploader). Optional on some forms — missing is fine.
        resume = page.query_selector(
            "spl-dropzone input[type='file'], input#file-input[type='file']")
        if resume:
            resume.set_input_files(str(self.documents["resume"]))
            page.wait_for_timeout(4000)  # upload + any parse settle

        held = self._fill_step_one(page)

        # Cover letter → "Message to the Hiring Team"
        msg = page.query_selector("textarea[id='hiring-manager-message-input']")
        if msg and self.documents.get("cover_letter_text"):
            try:
                if not msg.input_value():
                    msg.fill(self.documents["cover_letter_text"])
                    self.pause()
            except Exception:
                pass

        # Wizard: fill current page with the shared passes, advance on Next,
        # submit at the end. ONE refill pass per stuck page, then escalate
        # (same loop-guard philosophy as Workday).
        retried_step = None
        for _ in range(self.MAX_STEPS):
            held += self._generic_text_pass(page)
            held += self.handle_native_selects(page)
            held += self.handle_radio_groups(page)
            held += self.handle_checkbox_groups(page)
            self.handle_consent_checkboxes(page)
            self.handle_spl_consents(page)
            if held:
                return self.escalate_now(
                    page, "Essay/low-confidence questions held for your review",
                    extra={"held_questions": held})

            if self.detect_captcha(page):
                return self.escalate_now(page, "CAPTCHA before submit")

            submit = self._submit_button(page)
            if submit:
                return self._submit(page, submit)

            nxt = self._visible(page, "spl-button:has-text('Next'), "
                                      "button:has-text('Next')")
            if not nxt:
                return self.escalate_now(
                    page, "No Next/Submit control found on this step")
            signature = self._step_signature(page)
            nxt.click()
            page.wait_for_timeout(2500)
            if self._step_signature(page) == signature:
                # Step didn't advance → validation. ng-invalid hosts carry
                # the failing fields' labels.
                invalid = self._invalid_labels(page)
                if retried_step == signature:
                    return self.escalate_now(
                        page, f"Step won't advance — invalid fields: {invalid[:5]}",
                        extra={"invalid_fields": invalid})
                retried_step = signature  # one refill pass, then escalate
            else:
                retried_step = None
        return self.escalate_now(
            page, f"Wizard exceeded {self.MAX_STEPS} steps — bailing out")

    # ---------- step 1: identity by stable id ----------
    def _fill_step_one(self, page) -> list[dict]:
        ident = self.profile["identity"]
        held: list[dict] = []
        id_map = {
            "first-name-input": ident["first_name"],
            "last-name-input": ident["last_name"],
            "email-input": ident["email"],
            "confirm-email-input": ident["email"],
            "linkedin-input": ident.get("linkedin"),
            # Portfolio/website questions answer with the GitHub URL (user
            # directive) — but twitter/facebook are URL fields, and the
            # "None" standing answer is for QUESTIONS, not URL inputs: left
            # blank (they're optional).
            "website-input": ident.get("portfolio"),
        }
        for el_id, value in id_map.items():
            if value is None:
                continue
            el = page.query_selector(f"input[id='{el_id}']")
            if not el:
                continue
            try:
                if not el.input_value():
                    el.fill(str(value))
                    self.pause()
            except Exception:
                continue

        tel = self._visible(page, "input[type='tel']")
        if tel:
            try:
                if not tel.input_value():
                    tel.fill(str(ident["phone"]))
                    self.pause()
            except Exception:
                pass

        held += self._fill_city_typeahead(page)
        return held

    def _fill_city_typeahead(self, page) -> list[dict]:
        """spl-autocomplete "City" — async typeahead, generated input id.
        Type the profile city keystroke-by-keystroke and poll for a fuzzy
        match (same pattern as the Ashby location combobox). The field is
        optional on the captured form: on failure it's cleared and skipped
        unless its label is starred required."""
        held: list[dict] = []
        for inp in page.query_selector_all("spl-autocomplete input"):
            try:
                if not inp.is_visible() or inp.input_value():
                    continue
                label = self.field_label(page, inp)
                if not _CITYISH_RE.search(label or ""):
                    continue
                ident = self.profile["identity"]
                candidates = [str(c) for c in (
                    ident.get("location"), ident.get("city")) if c]
                picked = False
                for typed in [str(q) for q in (ident.get("city"),) if q]:
                    inp.click()
                    inp.type(typed, delay=40)
                    option = self._poll_option(page, candidates)
                    if option is not None:
                        option.click()
                        self.pause()
                        picked = True
                        break
                if not picked:
                    inp.fill("")
                    page.keyboard.press("Escape")
                    if (label or "").rstrip().endswith("*"):
                        held.append({"question": label, "options": [],
                                     "draft_answer": ""})
            except Exception:
                try:
                    page.keyboard.press("Escape")
                except Exception:
                    pass
                continue
        return held

    def _poll_option(self, page, candidates: list[str], timeout_ms: int = 6000):
        """Poll the open suggestion menu for a fuzzy candidate match — a
        fixed wait races the backend search (lesson from the Greenhouse
        school picker)."""
        waited = 0
        while waited < timeout_ms:
            page.wait_for_timeout(300)
            waited += 300
            pairs = [(o, " ".join((o.inner_text() or "").split()))
                     for o in page.query_selector_all(
                         "[role='listbox'] [role='option'], [role='option']")
                     if o.is_visible()]
            pairs = [(o, t) for o, t in pairs if t]
            options = [t for _, t in pairs]
            for desired in candidates:
                idx = self.match_option(desired, options)
                if idx is None:
                    idx = self.fuzzy_index(desired, options)
                if idx is not None:
                    return pairs[idx][0]
            # A visible "No results"-style notice ends the wait early
            # (same early-exit the Ashby/Greenhouse pickers use).
            if not pairs:
                lb = page.query_selector("[role='listbox']")
                try:
                    if lb and lb.is_visible() and re.search(
                            r"no (results|options|matches)",
                            lb.inner_text() or "", re.I):
                        return None
                except Exception:
                    pass
        return None

    # ---------- generic per-step passes ----------
    _HANDLED_IDS = {"first-name-input", "last-name-input", "email-input",
                    "confirm-email-input", "linkedin-input", "facebook-input",
                    "twitter-input", "website-input",
                    "hiring-manager-message-input"}

    def _generic_text_pass(self, page) -> list[dict]:
        """Unfilled visible text inputs/textareas (screening questions on
        later steps): identity map for short labels, Claude for questions —
        the same split every other handler uses."""
        held: list[dict] = []
        # spl-autocomplete inner inputs must NEVER be plain-filled: after a
        # failed typeahead the field is empty, its label ("City") matches the
        # identity map, and fill() would stuff raw text into a structured
        # picker with no suggestion selected. The role/aria attributes on the
        # inner input are unverified, so exclusion is by membership, not
        # attributes.
        typeahead_ids = set()
        for ta in page.query_selector_all("spl-autocomplete input"):
            try:
                typeahead_ids.add(ta.get_attribute("id") or "")
            except Exception:
                continue
        typeahead_ids.discard("")
        for el in page.query_selector_all(
                "input[type='text'], input[type='email'], textarea"):
            try:
                if not el.is_visible() or el.input_value():
                    continue
                el_id = el.get_attribute("id") or ""
                if el_id in self._HANDLED_IDS or el_id in typeahead_ids:
                    continue
                if el.get_attribute("role") == "combobox" \
                        or el.get_attribute("aria-autocomplete"):
                    continue  # typeahead — never plain-fill
                label = self.field_label(page, el)
                label = re.sub(r"\*\s*$", "", label or "").strip()
                if not label:
                    continue
                value = self.identity_value(label)
                if value is not None:
                    el.fill(value)
                    self.pause()
                    continue
                draft = self.answer_custom_question(label)
                if draft["hold"]:
                    held.append({"question": label,
                                 "draft_answer": draft.get("answer", "")})
                else:
                    el.fill(draft["answer"])
                    self.pause()
            except Exception:
                continue
        return held

    def handle_spl_consents(self, page) -> None:
        """spl-checkbox consent boxes: the agreement sentence is slotted
        LIGHT-DOM text on the host, which base handle_consent_checkboxes'
        parentElement climb can't reach from inside the shadow root (the
        chain stops at the shadow boundary). Read the host's textContent
        instead and click the inner input's label like a human."""
        for host in page.query_selector_all("spl-checkbox"):
            try:
                inner = host.query_selector("input[type='checkbox']")
                if not inner or inner.is_checked():
                    continue
                text = " ".join((host.evaluate("el => el.textContent") or "").split())
                if not text or len(text) > 900:
                    continue
                if self._CONSENT_RE.search(text):
                    try:
                        inner.check(timeout=5000)
                    except Exception:
                        host.click()  # styled widget hides the input
                    self.pause()
            except Exception:
                continue

    # ---------- wizard navigation ----------
    @staticmethod
    def _submitish(text: str) -> bool:
        t = " ".join((text or "").split())
        return bool(_SUBMIT_TEXT_RE.match(t)) and not _PARTNER_RE.search(t)

    def _submit_button(self, page):
        for el in page.query_selector_all(
                "spl-button, button[type='submit'], button"):
            try:
                if el.is_visible() and self._submitish(el.inner_text() or ""):
                    return el
            except Exception:
                continue
        return None

    def _step_signature(self, page) -> str:
        """Cheap step fingerprint: URL + visible headings + field count.
        Used to detect a Next click that validation bounced."""
        try:
            heads = " | ".join(
                " ".join((h.inner_text() or "").split())
                for h in page.query_selector_all("h1, h2, h3") if h.is_visible())
            fields = len([e for e in page.query_selector_all("input, textarea, select")
                          if e.is_visible()])
            return f"{page.url} :: {heads} :: {fields}"
        except Exception:
            return page.url

    def _invalid_labels(self, page) -> list[str]:
        out = []
        for host in page.query_selector_all(
                "spl-input.ng-invalid, spl-textarea.ng-invalid, "
                "spl-phone-field.ng-invalid, spl-autocomplete.ng-invalid, "
                "spl-checkbox.ng-invalid, spl-form-field.ng-invalid"):
            try:
                if not host.is_visible():
                    continue
                inner = host.query_selector("input, textarea, select")
                label = self.field_label(page, inner) if inner else ""
                out.append(label or (host.evaluate("el => el.tagName") or "?"))
            except Exception:
                continue
        return out

    def _submit(self, page, submit) -> RunResult:
        if self.auto.get("supervised_mode", True):
            print("\n⏸  SUPERVISED MODE: review the browser window, then press "
                  "Enter to submit (or Ctrl+C to abort)...")
            try:
                input()
            except KeyboardInterrupt:
                return RunResult(status="held",
                                 reason="Aborted at supervised-mode gate")
        if self._confirmed(page):
            return RunResult(status="submitted",
                             reason="Confirmation detected (submitted during supervised gate)")
        try:
            submit.click()
        except Exception:
            page.wait_for_timeout(2000)
            if self._confirmed(page):
                return RunResult(status="submitted",
                                 reason="Confirmation detected (click raced navigation)")
            raise
        page.wait_for_timeout(4000)
        if self._confirmed(page):
            return RunResult(status="submitted", reason="Confirmation detected")
        invalid = self._invalid_labels(page)
        if invalid:
            return self.escalate_now(
                page, f"Submit blocked by invalid fields: {invalid[:5]}",
                extra={"invalid_fields": invalid})
        return self.escalate_now(
            page, "Submission unverified — no confirmation marker found")

    @staticmethod
    def _confirmed(page) -> bool:
        """Explicit phrases ONLY. A loose ("thank you" AND "apply") combo
        false-positives on the FORM page itself — "Apply With Indeed" puts
        "apply" in the content, and _submit checks _confirmed BEFORE
        clicking (the supervised-gate manual-submit path), so a stray
        "thank you" in privacy/cookie text would record a submission that
        never happened."""
        try:
            content = page.content().lower()
        except Exception:
            content = ""
        return ("application submitted" in content
                or "successfully submitted" in content
                or "has been submitted" in content
                or "thank you for applying" in content
                or "thank you for your application" in content)

    # ---------- helpers ----------
    def _dismiss_cookie_banner(self, page) -> None:
        """OneTrust overlay — decline non-essential (privacy-preserving)."""
        try:
            btn = page.query_selector("#onetrust-reject-all-handler")
            if btn and btn.is_visible():
                btn.click()
                page.wait_for_timeout(800)
        except Exception:
            pass
