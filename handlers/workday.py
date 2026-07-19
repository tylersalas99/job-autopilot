"""Workday handler (beta — needs a live shakeout run before trusting).

Workday (myworkdayjobs.com) renders a multi-page apply wizard behind a
per-company ACCOUNT — unlike Greenhouse/Lever/Ashby there is a create-account
/ sign-in step before any form. Accounts are created automatically with the
profile email and a generated password, stored per tenant host in
``workday_accounts.json`` (gitignored, chmod 600). If an account already
exists for the email (from a past manual application), the run escalates
asking you to add that password to the accounts file — it never guesses.

DOM contract (data-automation-id driven — these ids are stable across
tenants and wd1/wd3/wd5 pods, unlike Workday's obfuscated CSS classes):

- Job page: ``[data-automation-id='adventureButton']`` = the Apply button.
  It opens "Start Your Application" with links ``autofillWithResume`` /
  ``applyManually`` / ``useMyLastApplication``. We take Autofill with
  Resume (user 2026-07-18): upload the tailored PDF, Workday parses it and
  pre-fills My Experience.
- Auth: ``input[data-automation-id='email'|'password'|'verifyPassword']``,
  ``[data-automation-id='createAccountCheckbox']`` (terms),
  links ``createAccountLink``/``signInLink`` toggle the two forms. The
  verifyPassword field is the create-vs-sign-in tell. Auth success =
  the password input disappears (polled, never a fixed wait).
  The submit buttons (``signInSubmitButton``/``createAccountSubmitButton``)
  are ``aria-hidden tabindex=-2`` UNDER a ``click_filter`` overlay div
  (role=button, aria-label = button text, inside ``noCaptchaWrapper``)
  that intercepts pointer events — clicking the real button times out
  after 30s (Red Hat shakeout 2026-07-18). Always click the overlay
  (``_click_submit_control``); the password is saved to the accounts
  file BEFORE the click so a mid-creation crash can't lose it.
- Wizard: pages My Information → My Experience → Application Questions →
  Voluntary Disclosures → Self Identify → Review. Footer
  ``pageFooterNextButton`` advances ("Save and Continue" / "Continue" /
  "Review" / "Submit" — SUBMIT IS THE SAME BUTTON, detected by its text).
  Validation failures raise ``[data-automation-id='errorBanner']`` plus
  field-level ``errorMessage`` nodes; the fill passes are re-run once
  (the banner marks which required fields were missed), then escalate.
- My Information (Red Hat live capture 2026-07-18): the INPUTS carry no
  automation ids — the ``formField-*`` WRAPPERS do (``formField-source``,
  ``formField-legalName--firstName``/``--lastName``,
  ``formField-addressLine1``/``-city``/``-postalCode``,
  ``formField-countryRegion`` (state — full name, so profile carries
  ``state_full``), ``formField-phoneType``, ``formField-phoneNumber``,
  ``formField-candidateIsPreviousWorker``). Fill via wrapper→inner input,
  then the generic label loop for other tenants' variants.
- Radios (previous-worker etc.): real ``input[type=radio]`` behind styled
  overlay spans — ``check()`` on the input can time out; click the
  ``label[for=…]`` instead (``handle_wd_radios``, runs before the base
  group handler). Question text lives in ``fieldset > legend > label``.
- ``phone-sms-opt-in`` is a singleton checkbox (consent regex won't tick
  it) — answered from ``standard_answers.sms_opt_in``.
- Wizard step detection: ``progressBarActiveStep``'s text ("current step
  2 of 7 My Information") — h1/h2 is the JOB TITLE, identical on every
  page (that read every advance as 'stuck' on the Red Hat run).
- Dropdowns: ``button[aria-haspopup='listbox']`` whose text is the chosen
  value ("Select One" = unanswered). Options portal to <body> as
  ``[role='option']`` / ``[data-automation-id='promptOption']`` — page-wide
  query is correct (one menu open at a time), same as Ashby. BUT: selected
  multiselect PILLS also carry ``promptOption`` (nested under
  ``selectedItemList``) — the pre-answered Country Phone Code pill
  masqueraded as a menu option on the Red Hat run and clicking it would
  DESELECT it. ``_visible_options`` filters out anything under a
  ``selectedItemList``. Option ElementHandles go stale when the portal
  re-renders — ``_click_option`` re-queries by text and uses short click
  timeouts (a covered/detached element must fail fast into a held
  question, not eat 30s and get silently skipped by a broad except).
- Multiselects (How Did You Hear About Us, Field of Study, Skills):
  ``[data-automation-id*='multiselectInputContainer']`` wrapping a search
  input; a chosen value renders a ``selectedItem`` pill. Only the
  hear-about multiselect is answered (careers-page preference, typed
  queries "careers" → "company website"); anything else is left alone —
  if Workday requires it, the error-banner path escalates with the
  banner text rather than typing guesses into an open search.
- Dates: ``dateSectionMonth-input``/``Day``/``Year`` spinbutton inputs.
  Only the Self Identify / Voluntary Disclosures signature date is
  filled (today); experience dates come from resume autofill.
- Field labels: inputs live in ``[data-automation-id^='formField']``
  wrappers carrying the real <label> — base field_label's class-based
  fallback can't see through Workday's obfuscated classes.
- Resume upload: ``input[data-automation-id='file-upload-input-ref']``
  (a real file input). Upload is idempotent per page: skipped when the
  resume filename already appears in the page.
- Confirmation: ``[data-automation-id='applyFlowConfirmation']`` or
  "successfully applied"-style text. Extend, don't replace.

Account creation may trigger an email verification code — with a terminal
the run pauses for you to complete it in the open browser; headless/phone
runs escalate.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import string
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from handlers.base import _HEAR_ABOUT_RE, _PLACEHOLDER_OPTION, BaseHandler, RunResult

# Dropdown button text that means "nothing chosen yet"
_WD_PLACEHOLDER = re.compile(r"^(select one|select|search)?$", re.I)

_CONFIRMATION_MARKERS = (
    "successfully applied",
    "application has been submitted",
    "thank you for applying",
    "thanks for applying",
)


class WorkdayHandler(BaseHandler):
    ats_name = "workday"

    # ---------- account storage ----------
    def accounts_path(self) -> Path:
        p = self.cfg.get("paths", {}).get("workday_accounts")
        if p:
            return Path(p)
        return Path(self.cfg["paths"]["pending_dir"]).parent / "workday_accounts.json"

    def load_account(self, host: str) -> dict | None:
        path = self.accounts_path()
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8")).get(host)
        except Exception:
            return None

    def save_account(self, host: str, email: str, password: str) -> None:
        path = self.accounts_path()
        data = {}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        data[host] = {"email": email, "password": password,
                      "created_at": datetime.now(timezone.utc).isoformat()}
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        try:
            os.chmod(path, 0o600)  # plaintext credentials — owner-only
        except OSError:
            pass

    @staticmethod
    def generate_password(length: int = 16) -> str:
        """Meets Workday's usual policy: upper + lower + digit + special,
        well past the 8-char minimum."""
        pools = [string.ascii_lowercase, string.ascii_uppercase,
                 string.digits, "!@#$%^&*"]
        chars = [secrets.choice(p) for p in pools]
        alphabet = "".join(pools)
        chars += [secrets.choice(alphabet) for _ in range(max(length, 12) - len(chars))]
        secrets.SystemRandom().shuffle(chars)
        return "".join(chars)

    # ---------- workday standing answers ----------
    def choice_value(self, question: str) -> str | None:
        """Workday-specific rows first, then the shared table."""
        q = question.lower()
        ident = self.profile["identity"]
        std = self.profile["standard_answers"]
        if re.search(r"phone\s*device|device\s*type", q):
            return "Mobile"
        if re.search(r"country.*(phone|code)|phone.*country", q):
            return ident.get("country")
        # Address state/region dropdown wants the full name ("Texas", not "TX")
        if re.search(r"\bstate\b|province|county.*region", q):
            return ident.get("state_full") or ident.get("state")
        # "How would you describe yourself?" is Workday's standard race/
        # ethnicity phrasing on Voluntary Disclosures — resolves via the
        # standing Hispanic/Latino answer (variant matching in answer_choice).
        if re.search(r"how would you describe yourself", q):
            return std.get("race_ethnicity")
        if re.search(r"opt.?in.*(text|sms)|text.*opt.?in", q):
            return "Yes" if std.get("sms_opt_in", True) else "No"
        # "Have you ever worked for Red Hat?" (Red Hat 2026-07-18) — the
        # (for|at|by) tail keeps questionnaire asks like "How long have you
        # worked WITH Python?" out of this row.
        if re.search(r"(ever|previous(?:ly)?).{0,30}(worked|employed|been employed)"
                     r"\s+(for|at|by|here)|"
                     r"currently (employed|work(?:ing)?)\s*(by|for|at)|"
                     r"former (employee|worker|associate)", q):
            history = " ".join(j.get("company", "").lower()
                               for j in self.profile.get("work_history", []))
            company = (self.posting.company or "").lower()
            if company and company in history:
                return None  # actually a former employer — never auto-answer No
            return "No"
        return super().choice_value(question)

    def standard_value(self, label_text: str) -> str | None:
        """Workday-only identity rows (base has no address concept)."""
        label = label_text.lower()
        ident = self.profile["identity"]
        table = [
            (r"address line\s*1|street address", ident.get("address_line1")),
            (r"postal|zip", ident.get("postal_code")),
        ]
        for pattern, value in table:
            if re.search(pattern, label):
                v = str(value) if value is not None else None
                return None if (v and v.startswith("TODO")) else v
        return super().standard_value(label_text)

    # ---------- labels ----------
    @staticmethod
    def _wd_label(el) -> str:
        """Label via the formField wrapper — Workday's CSS classes are
        obfuscated, so base field_label's [class*=field] fallback is blind."""
        try:
            text = el.evaluate(
                "el => { const f = el.closest(\"[data-automation-id^='formField']\");"
                " const l = f && f.querySelector('label');"
                " return l ? l.innerText : ''; }")
            # required-marker abbr renders as a trailing "*" in innerText
            return " ".join((text or "").split()).rstrip("*").strip()
        except Exception:
            return ""

    def question_for(self, page, el) -> str:
        return self._wd_label(el) or self.field_label(page, el)

    def field_label(self, page, el) -> str:
        """Route ALL base handlers (native selects, radios, checkboxes)
        through the formField-wrapper label first — base's class-based
        fallback is blind to Workday's obfuscated CSS."""
        return self._wd_label(el) or super().field_label(page, el)

    # ---------- page chrome ----------
    def _dismiss_cookie_banner(self, page) -> None:
        for sel in ("button#onetrust-accept-btn-handler",
                    "[data-automation-id='legalNoticeAcceptButton']",
                    "button:has-text('Accept Cookies')"):
            try:
                el = self._visible(page, sel)
                if el:
                    el.click()
                    page.wait_for_timeout(500)
                    return
            except Exception:
                continue

    def _page_step(self, page) -> str:
        """Which wizard page we're on. MUST come from the progress bar's
        active step ("current step 2 of 7 My Information") — h1/h2 is the
        job title, identical on every page, which made every advance look
        'stuck' on the Red Hat shakeout run."""
        el = self._visible(page, "[data-automation-id='progressBarActiveStep']")
        if el:
            try:
                text = " ".join((el.inner_text() or "").split())
                if text:
                    return text
            except Exception:
                pass
        for sel in ("[data-automation-id='pageHeaderTitle']", "h2", "h1", "h3"):
            el = self._visible(page, sel)
            if el:
                try:
                    text = " ".join((el.inner_text() or "").split())
                except Exception:
                    continue
                if text:
                    return text
        return ""

    def _error_banner(self, page) -> str:
        el = self._visible(
            page, "[data-automation-id='errorBanner'], "
                  "[data-automation-id='pageLevelErrorBanner'], "
                  "[data-automation-id='alertMessage'], [role='alert']")
        if not el:
            return ""
        try:
            text = " ".join((el.inner_text() or "").split())
        except Exception:
            return ""
        # aria-live alert nodes also announce non-errors (step changes,
        # upload success) — only error-ish text is a validation failure.
        return text if re.search(r"error|required|must have|fix", text, re.I) else ""

    def _field_errors(self, page) -> list[str]:
        out = []
        for el in page.query_selector_all("[data-automation-id='errorMessage']"):
            try:
                if el.is_visible():
                    text = " ".join((el.inner_text() or "").split())
                    if text:
                        out.append(text)
            except Exception:
                continue
        return out[:8]

    def _on_wizard(self, page) -> bool:
        # The AUTH page also renders the progress bar — its step 1 is
        # "Create Account/Sign In" (Travelers 2026-07-18), and the bar can
        # render before the form inputs. A visible password field always
        # means auth, never the wizard.
        if self._password_input(page):
            return False
        return bool(page.query_selector("[data-automation-id='progressBar']")
                    or page.query_selector("[data-automation-id='pageFooterNextButton']"))

    # ---------- auth ----------
    @staticmethod
    def _password_input(page):
        for el in page.query_selector_all("input[data-automation-id='password']"):
            try:
                if el.is_visible():
                    return el
            except Exception:
                continue
        return None

    def _sign_in_form_present(self, page) -> bool:
        return bool(self._visible(page, "input[data-automation-id='email']")
                    and self._password_input(page))

    def _create_mode(self, page) -> bool:
        return bool(self._visible(page, "input[data-automation-id='verifyPassword']"))

    def _auth_settled(self, page, timeout_ms: int = 15000) -> bool:
        """Auth success = the password field goes away (navigation into the
        wizard or back to the start screen). Polled — a fixed wait races
        Workday's redirect chain."""
        waited = 0
        while waited < timeout_ms:
            page.wait_for_timeout(500)
            waited += 500
            if not self._password_input(page):
                return True
        return False

    def _click_automation_id(self, page, aid: str) -> bool:
        el = self._visible(page, f"[data-automation-id='{aid}']")
        if el:
            el.click()
            self.pause()
            return True
        return False

    def _click_submit_control(self, page, aid: str, label: str) -> bool:
        """Auth submit buttons are overlaid by a ``click_filter`` div
        (role=button, aria-label = the button text, inside a
        ``noCaptchaWrapper`` — bot filtering) that INTERCEPTS pointer
        events: clicking the real <button aria-hidden tabindex=-2>
        underneath times out after 30s (Red Hat shakeout 2026-07-18).
        Click the overlay like a human would; fall back to the button
        only when no overlay exists."""
        overlay = self._visible(
            page, f"[data-automation-id='click_filter'][aria-label='{label}']")
        if overlay:
            overlay.click()
            self.pause()
            return True
        return self._click_automation_id(page, aid)

    def _fill_visible(self, page, selector: str, value: str | None) -> bool:
        """Visible-first fill — the sign-in and create-account forms coexist
        in the DOM (one hidden), and fill() on a hidden twin hangs."""
        if value is None:
            return False
        el = self._visible(page, selector)
        if el:
            el.fill(str(value))
            self.pause()
            return True
        return False

    @staticmethod
    def _tick_create_checkbox(page) -> None:
        """Terms checkbox on the create-account form (present on Red Hat,
        absent on Travelers)."""
        cb = page.query_selector("input[data-automation-id='createAccountCheckbox']")
        if not cb:
            return
        try:
            if not cb.is_checked():
                if cb.is_visible():
                    cb.check()
                else:  # styled widget hides the input behind its label
                    cb.evaluate("el => (el.closest('label') || el).click()")
        except Exception:
            pass

    def ensure_authenticated(self, page) -> RunResult | None:
        """Sign in with stored credentials, else create the account and store
        the generated password. Returns an escalation RunResult on failure,
        None on success."""
        host = urlparse(page.url).netloc or urlparse(self.posting.final_url).netloc
        email = self.profile["identity"]["email"]
        acct = self.load_account(host)

        if acct:
            if self._create_mode(page):  # accounts file wins — switch to sign-in
                self._click_automation_id(page, "signInLink")
                page.wait_for_timeout(1000)
            self._fill_visible(page, "input[data-automation-id='email']",
                               acct.get("email", email))
            pw = self._password_input(page)
            if pw:
                pw.fill(acct["password"])
            self._click_submit_control(page, "signInSubmitButton", "Sign In")
            if self._auth_settled(page):
                return None
            # A stored credential can predate a FAILED creation (the password
            # is saved BEFORE the create click, deliberately) — if the
            # create form is reachable, create the account with the SAME
            # stored password instead of dead-ending on sign-in.
            if not self._create_mode(page):
                self._click_automation_id(page, "createAccountLink")
                page.wait_for_timeout(1500)
            if self._create_mode(page):
                self._fill_visible(page, "input[data-automation-id='email']",
                                   acct.get("email", email))
                pw = self._password_input(page)
                if pw:
                    pw.fill(acct["password"])
                self._fill_visible(
                    page, "input[data-automation-id='verifyPassword']",
                    acct["password"])
                self._tick_create_checkbox(page)
                self._click_submit_control(page, "createAccountSubmitButton",
                                           "Create Account")
                if self._auth_settled(page):
                    return None
            return self.escalate_now(
                page, f"Workday sign-in failed for {host} — check the password "
                      f"in {self.accounts_path().name}")

        # No stored account → create one
        if not self._create_mode(page):
            self._click_automation_id(page, "createAccountLink")
            page.wait_for_timeout(1500)
        if not self._create_mode(page):
            return self.escalate_now(
                page, "Could not reach Workday's Create Account form")
        password = self.generate_password()
        self._fill_visible(page, "input[data-automation-id='email']", email)
        pw = self._password_input(page)
        if pw:
            pw.fill(password)
        self._fill_visible(page, "input[data-automation-id='verifyPassword']", password)
        self._tick_create_checkbox(page)
        # Save BEFORE clicking: if the click lands but the run dies before
        # settle (crash, timeout), the account exists — losing the password
        # here would lock us out of the tenant. A saved credential for a
        # never-created account just fails sign-in next run and escalates.
        self.save_account(host, email, password)
        self._click_submit_control(page, "createAccountSubmitButton", "Create Account")
        if not self._auth_settled(page):
            err = self._error_banner(page) or " ".join(self._field_errors(page))
            if re.search(r"already|exists|in use", err, re.I):
                return self.escalate_now(
                    page, f"Workday account already exists for {email} on {host} "
                          f"— add its password to {self.accounts_path().name} "
                          "under this host and re-run")
            return self.escalate_now(
                page, f"Workday account creation failed: {err[:300] or 'no error shown'}")
        print(f"  ✓ Created Workday account for {host} "
              f"(credentials → {self.accounts_path().name})")
        return None

    def _maybe_email_verification(self, page) -> RunResult | None:
        """Some tenants email a verification code at account creation. With a
        terminal, wait for the user to finish it in the open browser;
        otherwise escalate (keep-open in main.py still applies)."""
        code_input = page.query_selector(
            "input[data-automation-id*='verif' i], input[name*='verif' i]")
        if not code_input:
            return None
        try:
            if not code_input.is_visible():
                return None
        except Exception:
            return None
        if sys.stdin.isatty():
            print("\n⏸  Workday wants an email verification code "
                  f"(sent to {self.profile['identity']['email']}).")
            try:
                input("   Complete it in the browser window, then press Enter here...")
            except (KeyboardInterrupt, EOFError):
                return RunResult(status="held", reason="Aborted at email verification")
            return None
        return self.escalate_now(page, "Workday email verification code required")

    # ---------- reaching the form ----------
    def _reach_application(self, page) -> RunResult | None:
        """Job page → Apply → Autofill with Resume → (auth) → wizard. The
        order varies by tenant (some demand sign-in before showing the
        options), so this is a small state loop, not a fixed sequence."""
        for _ in range(8):
            self._dismiss_cookie_banner(page)
            if self._sign_in_form_present(page):
                res = self.ensure_authenticated(page)
                if res:
                    return res
                page.wait_for_timeout(2000)
                continue
            res = self._maybe_email_verification(page)
            if res:
                return res
            if self._on_wizard(page):
                return None
            link = self._visible(
                page, "a[data-automation-id='autofillWithResume'], "
                      "[data-automation-id='autofillWithResume']")
            if link:
                link.click()
                page.wait_for_timeout(2500)
                continue
            btn = self._visible(page, "[data-automation-id='adventureButton']")
            if btn:
                btn.click()
                page.wait_for_timeout(2500)
                continue
            page.wait_for_timeout(1500)
        return self.escalate_now(page, "Could not reach the Workday application form")

    # ---------- filling ----------
    # data-automation-id → identity key for My Information / Self Identify.
    # TODO-valued profile entries are skipped (never guessed) — the field
    # stays empty and the error-banner path escalates with Workday's own
    # message naming it.
    _IDENTITY_IDS = [
        # formField-* wrappers (Red Hat live capture 2026-07-18 — the inner
        # inputs carry no automation ids of their own)
        ("formField-legalName--firstName", "first_name"),
        ("formField-legalName--lastName", "last_name"),
        ("formField-addressLine1", "address_line1"),
        ("formField-city", "city"),
        ("formField-postalCode", "postal_code"),
        ("formField-phoneNumber", "phone"),
        # older/other tenant variants
        ("legalNameSection_firstName", "first_name"),
        ("legalNameSection_lastName", "last_name"),
        ("addressSection_addressLine1", "address_line1"),
        ("addressSection_city", "city"),
        ("addressSection_postalCode", "postal_code"),
        ("phone-number", "phone"),
        ("name", "full_name"),  # Self Identify signature field
    ]

    def _fill_known_identity(self, page) -> None:
        ident = self.profile["identity"]
        for aid, key in self._IDENTITY_IDS:
            value = ident.get(key)
            if value is None or str(value).startswith("TODO"):
                continue
            el = self._visible(
                page, f"input[data-automation-id='{aid}'], "
                      f"[data-automation-id='{aid}'] input")
            if not el:
                continue
            try:
                if el.input_value():
                    continue  # autofilled (resume parse / account) — leave it
                el.fill(str(value))
                self.pause()
            except Exception:
                continue

    def _is_questionnaire(self, page) -> bool:
        return "question" in self._page_step(page).lower()

    def _fill_text_inputs(self, page) -> list[dict]:
        held = []
        questionnaire = self._is_questionnaire(page)
        for el in page.query_selector_all(
                "input[type='text'], input[type='email'], input[type='tel'], "
                "input:not([type]), textarea"):
            try:
                if not el.is_visible() or el.input_value():
                    continue
                if el.get_attribute("role") in ("combobox", "spinbutton"):
                    continue  # dropdown/multiselect search or date part
                aid = el.get_attribute("data-automation-id") or ""
                if "dateSection" in aid or "file-upload" in aid:
                    continue
                if aid == "beecatcher":
                    continue  # bot-trap honeypot (name="website", Travelers
                              # 2026-07-18) — filling it flags the session
                # Multiselect search inputs carry no role attribute — typing
                # an "answer" into one is junk in an open search box.
                if el.evaluate(
                        "el => !!el.closest(\"[data-automation-id='multiSelectContainer'],"
                        " [data-automation-id='multiselectInputContainer']\")"):
                    continue
                label = self.question_for(page, el)
                if not label:
                    continue
                value = self.identity_value(label)
                if value is not None:
                    el.fill(value)
                    self.pause()
                    continue
                # Long labels and questionnaire-page fields are questions;
                # short unmapped labels elsewhere are left for the
                # error-banner check rather than guessed at.
                if len(label) > 60 or label.rstrip().endswith("?") or questionnaire:
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

    @staticmethod
    def _visible_options(page) -> list[tuple]:
        """(element, text) pairs for the OPEN menu only. Selected pills reuse
        the promptOption automation-id (nested under selectedItemList) —
        clicking one deselects it, so they must be filtered out."""
        pairs = []
        seen = set()
        for o in page.query_selector_all(
                "[data-automation-id='promptOption'], [role='listbox'] [role='option']"):
            try:
                if not o.is_visible():
                    continue
                if o.evaluate(
                        "el => !!el.closest(\"[data-automation-id='selectedItemList']\")"):
                    continue  # selected pill, not a menu option
                text = " ".join((o.inner_text() or "").split())
            except Exception:
                continue
            if text and text not in seen:
                seen.add(text)
                pairs.append((o, text))
        return pairs

    def _poll_options(self, page, timeout_ms: int) -> list[tuple]:
        """Poll the open menu — lists render async, fixed waits lose races."""
        waited = 0
        while waited < timeout_ms:
            page.wait_for_timeout(300)
            waited += 300
            pairs = self._visible_options(page)
            if pairs:
                return pairs
        return []

    def _click_option(self, page, text: str) -> bool:
        """Click the menu option with exactly `text`. The portal re-renders as
        results settle, detaching previously captured ElementHandles — so
        re-query by text and fail FAST (5s) instead of the default 30s that
        a broad except then turns into a silently skipped field."""
        for _ in range(2):
            for o, t in self._visible_options(page):
                if t != text:
                    continue
                try:
                    o.click(timeout=5000)
                    self.pause()
                    return True
                except Exception:
                    break  # stale/covered handle — re-query and retry once
            page.wait_for_timeout(400)
        return False

    def handle_dropdowns(self, page) -> list[dict]:
        """Answer unanswered Workday dropdowns (button[aria-haspopup=listbox]).
        Resolution is profile-first via answer_choice; unknowns are held."""
        held = []
        for btn in page.query_selector_all("button[aria-haspopup='listbox']"):
            try:
                if not btn.is_visible():
                    continue
                current = " ".join((btn.inner_text() or "").split())
                if current and not _WD_PLACEHOLDER.match(current) \
                        and not _PLACEHOLDER_OPTION.match(current):
                    continue  # already answered
                question = self.question_for(page, btn)
                if not question:
                    continue
                btn.click()
                pairs = self._poll_options(page, 3000)
                if not pairs:
                    page.keyboard.press("Escape")
                    continue
                options = [t for _, t in pairs]
                res = self.answer_choice(question, options)
                if res["hold"] or res["value"] is None \
                        or not self._click_option(page, res["value"]):
                    held.append({"question": question, "options": options[:30],
                                 "draft_answer": res["value"] or ""})
                    page.keyboard.press("Escape")
            except Exception:
                try:
                    page.keyboard.press("Escape")
                except Exception:
                    pass
                continue
        return held

    def handle_multiselects(self, page) -> list[dict]:
        """Only the hear-about multiselect is answered (careers-page
        preference, user 2026-07-14). Other multiselects (skills, field of
        study) are left alone — typing guesses into an open-ended search is
        worse than escalating via the error banner if Workday requires one."""
        held = []
        seen_inputs = set()
        for cont in page.query_selector_all(
                "[data-automation-id*='multiselectInputContainer'], "
                "[data-automation-id*='multiSelectContainer']"):
            try:
                if not cont.is_visible():
                    continue
                if cont.query_selector("[data-automation-id*='selectedItem']"):
                    continue  # already answered (pill rendered)
                inp = cont.query_selector("input")
                # The outer multiSelectContainer NESTS the inner
                # multiselectInputContainer — both match this query, so
                # dedupe by the shared search input.
                key = inp.get_attribute("id") if inp else None
                if key and key in seen_inputs:
                    continue
                if key:
                    seen_inputs.add(key)
                question = self.question_for(page, inp or cont)
                if not question or not _HEAR_ABOUT_RE.search(question):
                    continue
                (inp or cont).click()
                pairs = self._poll_options(page, 2500)
                if not pairs and inp:
                    for query in ("careers", "jobs", "company website"):
                        inp.fill("")
                        inp.type(query, delay=40)  # keystrokes drive the search
                        pairs = self._poll_options(page, 4000)
                        if pairs:
                            break
                # Source lists can NEST: clicking a category ("Company
                # Website") opens a leaf submenu — a click only counts once
                # a selectedItem pill renders. Descend up to two levels.
                picked = False
                for _level in range(2):
                    options = [t for _, t in pairs]
                    idx = self.careers_option_index(options)
                    if idx is None:
                        idx = self.match_option(
                            self.profile["standard_answers"].get("how_did_you_hear"),
                            options)
                    if idx is None or not self._click_option(page, options[idx]):
                        break
                    page.wait_for_timeout(800)
                    if cont.query_selector("[data-automation-id*='selectedItem']"):
                        picked = True  # pill rendered — actually selected
                        break
                    pairs = self._poll_options(page, 2500)  # submenu leaves
                    if not pairs:
                        break
                page.keyboard.press("Escape")
                if not picked:
                    held.append({"question": question,
                                 "options": [t for _, t in pairs][:30],
                                 "draft_answer": ""})
            except Exception:
                try:
                    page.keyboard.press("Escape")
                except Exception:
                    pass
                continue
        return held

    @staticmethod
    def _wd_legend(el) -> str:
        try:
            text = el.evaluate(
                "el => { const f = el.closest('fieldset');"
                " const lg = f && f.querySelector('legend');"
                " return lg ? lg.innerText : ''; }")
            return " ".join((text or "").split()).rstrip("*").strip()
        except Exception:
            return ""

    def handle_wd_radios(self, page) -> list[dict]:
        """Workday radios sit behind styled overlay spans — check() on the
        input can time out (swallowed by base's broad except: the field is
        silently skipped, which cost the Red Hat run its previous-worker
        answer). Click the label[for=…] like a human. Runs BEFORE the base
        group handler; whatever this answers, base then skips as checked."""
        held = []
        groups: dict[str, list] = {}
        for r in page.query_selector_all("input[type='radio']"):
            name = r.get_attribute("name") or ""
            if name:
                groups.setdefault(name, []).append(r)
        for name, els in groups.items():
            try:
                if any(e.is_checked() for e in els):
                    continue
                labeled = []
                for e in els:
                    rid = e.get_attribute("id")
                    lab = page.query_selector(f"label[for='{rid}']") if rid else None
                    text = " ".join((lab.inner_text() or "").split()) if lab else ""
                    labeled.append((lab, text))
                options = [t for _, t in labeled if t]
                if len(options) < 2:
                    continue
                question = self._wd_legend(els[0])
                if not question or question in options:
                    continue  # leave for the base handler's hold logic
                res = self.answer_choice(question, options)
                if res["hold"] or res["value"] is None:
                    held.append({"question": question, "options": options,
                                 "draft_answer": res["value"] or ""})
                    continue
                for lab, t in labeled:
                    if t == res["value"] and lab is not None:
                        lab.click(timeout=5000)  # fail fast, never a silent 30s
                        self.pause()
                        break
            except Exception:
                continue
        return held

    def handle_sms_opt_in(self, page) -> None:
        """phone-sms-opt-in is a singleton checkbox the consent regex won't
        touch — answered from standard_answers.sms_opt_in (Yes, per user
        2026-07-12)."""
        if not self.profile["standard_answers"].get("sms_opt_in", True):
            return
        cb = page.query_selector("input[data-automation-id='phone-sms-opt-in']")
        if not cb:
            return
        try:
            if cb.is_checked():
                return
            if cb.is_visible():
                cb.check(timeout=5000)
            else:
                cb.evaluate("el => (el.closest('label') || el).click()")
            self.pause()
        except Exception:
            try:
                cb.evaluate("el => (el.closest('label') || el).click()")
            except Exception:
                pass

    def handle_disability_checkboxes(self, page) -> None:
        """Self Identify's disability status renders as a trio of checkboxes
        that may NOT share a name attribute (so the base same-name group
        handler can miss them). Exactly one — the profile's standing
        answer — is checked."""
        boxes = []
        for cb in page.query_selector_all(
                "[data-automation-id*='disability' i] input[type='checkbox']"):
            try:
                if cb.is_checked():
                    return  # already answered
                label = cb.evaluate(
                    "el => { const l = el.closest('label') ||"
                    " (el.id && document.querySelector(`label[for=\"${el.id}\"]`));"
                    " return (l ? l.innerText : '') || ''; }")
                boxes.append((cb, " ".join((label or "").split())))
            except Exception:
                continue
        boxes = [(cb, t) for cb, t in boxes if t]
        if len(boxes) < 2:
            return
        desired = self.profile["standard_answers"].get("disability_status")
        idx = self.match_option(desired, [t for _, t in boxes])
        if idx is None:
            return  # leave unanswered → error-banner path escalates
        cb = boxes[idx][0]
        try:
            if cb.is_visible():
                cb.check()
            else:
                cb.evaluate("el => (el.closest('label') || el).click()")
            self.pause()
        except Exception:
            pass

    @staticmethod
    def _ym(value) -> tuple[str, str] | None:
        """'2019-08' → ('2019', '08')."""
        m = re.match(r"(\d{4})-(\d{1,2})", str(value or ""))
        if not m:
            return None
        return m.group(1), f"{int(m.group(2)):02d}"

    @staticmethod
    def _seg_matches(seg, desired: str) -> bool:
        return (seg.input_value() or "").lstrip("0") == desired.lstrip("0")

    def _set_segment(self, panel, field_aid: str, seg_aid: str, desired: str) -> None:
        """Rewrite one MM/YYYY spinbutton segment if it differs.

        Clicking a date segment can open the CALENDAR POPUP, which swallows
        the keystrokes — the type-based rewrite silently changed nothing on
        the Travelers run (2026-07-18). Primary path: set the value through
        the native HTMLInputElement setter + input event (React's onChange
        reads e.target.value — the standard controlled-input hack, same as
        base's hidden-select fallback). Keyboard path is the fallback, and
        every attempt is VERIFIED — an unfixed segment prints a warning and
        is left for the validation/escalation path, never silently skipped."""
        seg = panel.query_selector(
            f"[data-automation-id='{field_aid}'] "
            f"input[data-automation-id='{seg_aid}']")
        if not seg:
            return
        try:
            if self._seg_matches(seg, desired):
                return  # already correct ("1" == "01")
            seg.evaluate(
                "(el, v) => { const s = Object.getOwnPropertyDescriptor("
                "window.HTMLInputElement.prototype, 'value').set;"
                " s.call(el, v);"
                " el.dispatchEvent(new Event('input',  {bubbles: true}));"
                " el.dispatchEvent(new Event('change', {bubbles: true})); }",
                desired)
            self.pause()
            if self._seg_matches(seg, desired):
                return
            seg.click()
            seg.press("ControlOrMeta+a")
            seg.type(desired, delay=60)
            self.pause()
            if not self._seg_matches(seg, desired):
                print(f"  ⚠️  date segment {field_aid}/{seg_aid} resisted "
                      f"rewrite to {desired} — fix it in the browser")
        except Exception:
            pass

    def verify_experience_dates(self, page) -> None:
        """Workday's resume parse INVENTS dates (Red Hat 2026-07-18: Lugo
        01/2008–01/2019 instead of 08/2019–09/2022; education From 2022
        To 2005). The profile is ground truth — match each Work Experience
        panel by company text and rewrite wrong segments; education years
        come from education[0]. Panels are [role=group] with
        aria-labelledby 'Work-Experience-N-panel' / 'Education-N-panel'
        (the '-section' wrapper spans all panels — skipped)."""
        for panel in page.query_selector_all(
                "[role='group'][aria-labelledby*='Work-Experience-']"):
            try:
                if (panel.get_attribute("aria-labelledby") or "").endswith("-section"):
                    continue
                comp = panel.query_selector(
                    "[data-automation-id='formField-companyName'] input")
                if not comp:
                    continue
                text = self._norm(comp.input_value())
                job = None
                for j in self.profile.get("work_history", []):
                    jc = self._norm(j.get("company", ""))
                    if jc and text and (jc in text or text in jc):
                        job = j
                        break
                if job is None:
                    continue  # not a profile job — never touch it
                ym = self._ym(job.get("start"))
                if ym:
                    self._set_segment(panel, "formField-startDate",
                                      "dateSectionMonth-input", ym[1])
                    self._set_segment(panel, "formField-startDate",
                                      "dateSectionYear-input", ym[0])
                end = str(job.get("end", "")).lower()
                ym = self._ym(job.get("end")) if end not in ("present", "current") \
                    else None
                if ym:
                    self._set_segment(panel, "formField-endDate",
                                      "dateSectionMonth-input", ym[1])
                    self._set_segment(panel, "formField-endDate",
                                      "dateSectionYear-input", ym[0])
            except Exception:
                continue
        edu = (self.profile.get("education") or [{}])[0]
        for panel in page.query_selector_all(
                "[role='group'][aria-labelledby*='Education-']"):
            try:
                if (panel.get_attribute("aria-labelledby") or "").endswith("-section"):
                    continue
                ym = self._ym(edu.get("started"))
                if ym:
                    self._set_segment(panel, "formField-firstYearAttended",
                                      "dateSectionYear-input", ym[0])
                ym = self._ym(edu.get("graduated"))
                if ym:
                    self._set_segment(panel, "formField-lastYearAttended",
                                      "dateSectionYear-input", ym[0])
            except Exception:
                continue

    def _fill_signature_date(self, page) -> None:
        """Self Identify / Voluntary Disclosures ask for today's date next to
        the signature. Experience/education dates come from resume autofill
        and are never touched here."""
        header = self._page_step(page).lower()
        if not re.search(r"self.?identif|disclosur", header):
            return
        today = date.today()
        for aid, value in (("dateSectionMonth-input", f"{today.month:02d}"),
                           ("dateSectionDay-input", f"{today.day:02d}"),
                           ("dateSectionYear-input", str(today.year))):
            el = self._visible(page, f"input[data-automation-id='{aid}']")
            if not el:
                continue
            try:
                if el.input_value():
                    continue
                el.click()
                el.type(value, delay=60)  # spinbuttons take keystrokes, not fill()
                self.pause()
            except Exception:
                continue

    def _upload_resume_if_asked(self, page) -> None:
        resume = self.documents["resume"]
        try:
            if Path(resume).name in page.content():
                return  # already uploaded (this page or resume-parse carryover)
        except Exception:
            pass
        file_input = page.query_selector(
            "input[data-automation-id='file-upload-input-ref'], input[type='file']")
        if not file_input:
            return
        try:
            file_input.set_input_files(str(resume))
            waited = 0
            while waited < 15000:  # poll for processing, never a fixed wait
                page.wait_for_timeout(500)
                waited += 500
                if page.query_selector(
                        "[data-automation-id='file-upload-successful']"):
                    break
        except Exception:
            pass

    def fill_page(self, page) -> list[dict]:
        held = []
        self._fill_known_identity(page)
        self.verify_experience_dates(page)   # fix resume-parse artifacts
        held += self._fill_text_inputs(page)
        # Travelers' questionnaire renders NATIVE <select>s (2026-07-18) —
        # Red Hat used button-dropdowns only, so this pass was missing and
        # every questionnaire answer fell to the user.
        held += self.handle_native_selects(page)
        held += self.handle_dropdowns(page)
        held += self.handle_multiselects(page)
        held += self.handle_wd_radios(page)      # label clicks; must run first
        held += self.handle_radio_groups(page)
        held += self.handle_checkbox_groups(page)
        self.handle_sms_opt_in(page)
        self.handle_disability_checkboxes(page)
        self.handle_consent_checkboxes(page)
        self._fill_signature_date(page)
        return held

    # ---------- wizard ----------
    def _next_button(self, page, timeout_ms: int = 20000):
        """VISIBLE footer button, polled. The button exists in the DOM but is
        hidden behind a spinner while Workday processes the uploaded resume —
        a one-shot check escalated a healthy run ('No Save and Continue /
        Submit button found', Red Hat run #4, 2026-07-18). Page content
        renders alongside the footer, so this also gates fill passes on a
        freshly loaded page."""
        waited = 0
        while waited < timeout_ms:
            btn = self._visible(page, "[data-automation-id='pageFooterNextButton']")
            if btn:
                return btn
            page.wait_for_timeout(500)
            waited += 500
        return None

    def _advance_state(self, page, prev_header: str, timeout_ms: int = 15000) -> str:
        """After clicking Save and Continue: 'advanced' | 'error' | 'stuck'.
        Returns the moment the step changes or an error shows — the timeout
        only bounds the do-nothing case (halved from 30s at the user's
        request; resume parsing is already waited out at upload time, and a
        premature 'stuck' just lands on the resumable pause prompt)."""
        waited = 0
        while waited < timeout_ms:
            page.wait_for_timeout(500)
            waited += 500
            # Step change is checked FIRST: Workday can land on the next page
            # with a validation banner already up (Red Hat run #3 read that
            # as an error on the PREVIOUS step) — the caller refills the new
            # page anyway, so advancing wins.
            header = self._page_step(page)
            if header and header != prev_header:
                return "advanced"
            if self._error_banner(page):
                return "error"
            # Some pages (Voluntary Disclosures, Red Hat) surface only
            # FIELD-level errorMessage nodes, no page-level banner — that
            # must trigger the refill-and-retry branch, not read as stuck.
            if self._field_errors(page):
                return "error"
        return "stuck"

    def _wait_form_settle(self, page, timeout_ms: int = 10000) -> None:
        """The footer button renders before the form fields do — filling on
        arrival found ZERO dropdowns on Voluntary Disclosures (Red Hat
        2026-07-18). Wait until the formField count is nonzero and stable
        across two polls before touching the page."""
        prev = -1
        waited = 0
        while waited < timeout_ms:
            try:
                count = len(page.query_selector_all(
                    "[data-automation-id^='formField']"))
            except Exception:
                count = 0
            if count and count == prev:
                return
            prev = count
            page.wait_for_timeout(600)
            waited += 600

    @staticmethod
    def _print_held(held: list[dict]) -> None:
        for q in held[:6]:
            print(f"   ❓ {q.get('question', '?')[:110]}")

    def _offer_resume(self, result: RunResult) -> bool:
        """After an escalation, offer to keep going in the SAME run (user
        2026-07-18): the case is already written and the browser still shows
        the live page — fix/fill whatever blocked us, press Enter, and the
        wizard re-enters from the current page (fill passes skip everything
        already answered). 's' or no terminal (phone runs) = stop, exactly
        the old behavior. Held questions still always pause: this never
        auto-answers anything."""
        if not sys.stdin.isatty():
            return False
        print(f"\n⏸  Escalated: {result.reason}")
        case = result.details.get("case")
        if case:
            print(f"   Case written to {case}")
        print("   Fix or fill things in the browser window, then press Enter to "
              "RETRY from the current page (or type 's' to stop here).")
        try:
            return input("> ").strip().lower() != "s"
        except (KeyboardInterrupt, EOFError):
            return False

    def apply(self, page) -> RunResult:
        page.goto(self.posting.final_url, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)
        self._dismiss_cookie_banner(page)
        if self.detect_captcha(page):
            return self.escalate_now(page, "CAPTCHA present on load")
        while True:
            result = self._attempt(page)
            if result.status != "escalated" or not self._offer_resume(result):
                return result

    def _attempt(self, page) -> RunResult:
        """One pass: reach the form (idempotent — _reach_application detects
        wizard/sign-in/apply-links from whatever state the page is in, so a
        retry needs no reload), then walk the wizard."""
        res = self._reach_application(page)
        if res:
            return res

        last_step, same_step = "", 0
        recovered = False
        for _ in range(12):  # wizard pages, with slack for error retries
            res = self._maybe_email_verification(page)
            if res:
                return res
            # Wait for the page to actually render (footer + content load
            # async after the progress-bar shell) BEFORE filling anything.
            self._next_button(page)
            self._wait_form_settle(page)
            step = self._page_step(page)
            # Loop guard: repeated full fill passes over the SAME page look
            # like endless scrolling (user 2026-07-18) and add nothing —
            # ONE repeat pass, then stop and escalate (tightened from 3 at
            # the user's request — the pause prompt makes retries cheap).
            if step and step == last_step:
                same_step += 1
                if same_step >= 2:
                    return self.escalate_now(
                        page, f"Loop guard: no progress after repeated passes "
                              f"on '{step}'")
            else:
                last_step, same_step = step, 0
            print(f"  → {step or 'unknown page'}")
            self._upload_resume_if_asked(page)
            held = self.fill_page(page)
            if held:
                self._print_held(held)
                return self.escalate_now(
                    page, "Essay/low-confidence questions held for your review",
                    extra={"held_questions": held})

            next_btn = self._next_button(page)
            if not next_btn:
                # Fell off the wizard entirely (session bounce → Candidate
                # Home, Red Hat 2026-07-18: page had only navigation, no
                # footer). Re-enter from the posting URL — once.
                if not recovered and not self._on_wizard(page):
                    recovered = True
                    page.goto(self.posting.final_url, wait_until="domcontentloaded")
                    page.wait_for_timeout(3000)
                    res = self._reach_application(page)
                    if res:
                        return res
                    continue
                return self.escalate_now(page, "No Save and Continue / Submit button found")
            label = " ".join((next_btn.inner_text() or "").split()).lower()

            if "submit" in label:
                if self.detect_captcha(page):
                    return self.escalate_now(page, "CAPTCHA before submit")
                if self.auto.get("supervised_mode", True):
                    print("\n⏸  SUPERVISED MODE: review the browser window, then press "
                          "Enter to submit (or Ctrl+C to abort)...")
                    try:
                        input()
                    except KeyboardInterrupt:
                        return RunResult(status="held",
                                         reason="Aborted at supervised-mode gate")
                next_btn.click()
                page.wait_for_timeout(6000)
                content = page.content().lower()
                if any(m in content for m in _CONFIRMATION_MARKERS) \
                        or page.query_selector(
                            "[data-automation-id='applyFlowConfirmation']"):
                    return RunResult(status="submitted", reason="Confirmation detected")
                err = self._error_banner(page)
                if err:
                    return self.escalate_now(
                        page, f"Submit rejected: {err[:300]}",
                        extra={"field_errors": self._field_errors(page)})
                return self.escalate_now(
                    page, "Submission unverified — no confirmation marker found")

            header = self._page_step(page)
            next_btn.click()
            state = self._advance_state(page, header)
            if state == "error":
                # The banner marks which required fields were missed — the
                # fill passes see them now (error styling/aria). One retry,
                # after letting the field-level rerender settle.
                page.wait_for_timeout(1500)
                held = self.fill_page(page)
                if held:
                    self._print_held(held)
                    return self.escalate_now(
                        page, "Held questions after validation errors",
                        extra={"held_questions": held})
                current = self._page_step(page)
                retry_btn = self._next_button(page)
                if retry_btn:
                    retry_btn.click()
                    state = self._advance_state(page, current, timeout_ms=15000)
                if state != "advanced":
                    return self.escalate_now(
                        page, f"Workday validation errors on '{current}': "
                              f"{self._error_banner(page)[:200]}",
                        extra={"field_errors": self._field_errors(page)})
            elif state == "stuck":
                return self.escalate_now(
                    page, f"Wizard did not advance past '{header}'")
        return self.escalate_now(page, "Wizard exceeded the expected number of pages")
