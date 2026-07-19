"""Lever handler.

Lever application forms live at jobs.lever.co/<company>/<id>/apply and are
single-page with stable input names: name, email, phone, org, urls[LinkedIn],
urls[GitHub], urls[Portfolio], comments (cover letter), resume upload, plus
"cards" for custom questions.
"""
from __future__ import annotations

from handlers.base import BaseHandler, RunResult


class LeverHandler(BaseHandler):
    ats_name = "lever"

    def apply(self, page) -> RunResult:
        url = self.posting.final_url
        if not url.rstrip("/").endswith("/apply"):
            url = url.rstrip("/") + "/apply"
        page.goto(url, wait_until="domcontentloaded")
        self.pause()

        if self.detect_captcha(page):
            return self.escalate_now(page, "CAPTCHA present on load")

        ident = self.profile["identity"]
        named = {
            "input[name='name']": ident["full_name"],
            "input[name='email']": ident["email"],
            "input[name='phone']": ident["phone"],
            "input[name='org']": self.profile["work_history"][0]["company"],
            "input[name='urls[LinkedIn]']": ident["linkedin"],
            "input[name='urls[GitHub]']": ident["github"],
            "input[name='urls[Portfolio]']": ident.get("portfolio"),
            "input[name='urls[Twitter]']": ident.get("twitter"),
            "input[name='location']": ident["location"],
        }
        for selector, value in named.items():
            self.fill_if_present(page, selector, value)

        resume_input = page.query_selector("input[name='resume'], input[type='file']")
        if not resume_input:
            return self.escalate_now(page, "No resume upload field found")
        resume_input.set_input_files(str(self.documents["resume"]))
        self.pause()
        page.wait_for_timeout(2500)  # Lever parses the resume client-side

        # Cover letter goes in the free-form comments box when present
        comments = page.query_selector("textarea[name='comments']")
        if comments and self.documents.get("cover_letter_text"):
            comments.fill(self.documents["cover_letter_text"])
            self.pause()

        # Custom question cards: textareas and text inputs inside .application-question
        held = []
        for container in page.query_selector_all(
            ".application-question, li[class*='application-question']"
        ):
            field = container.query_selector("textarea, input[type='text']")
            # Invisible text inputs are decoys — e.g. the pronouns widget's
            # hidden "Custom" field (#customPronounsTextField); fill() on
            # them times out. The real control is a checkbox group, handled
            # by handle_checkbox_groups below.
            if not field or not field.is_visible() or field.input_value():
                continue
            label_el = container.query_selector(
                ".application-label, .text, label"
            )
            question = (label_el.inner_text() or "").strip() if label_el else ""
            if not question:
                continue
            draft = self.answer_custom_question(question)
            if draft["hold"]:
                held.append({"question": question, "draft_answer": draft.get("answer", "")})
            else:
                field.fill(draft["answer"])
                self.pause()
        # Dropdowns (incl. EEO) and radio groups anywhere on the form
        held += self.handle_native_selects(page)
        held += self.handle_radio_groups(page)
        held += self.handle_checkbox_groups(page)
        self.handle_consent_checkboxes(page)
        if held:
            return self.escalate_now(
                page, "Essay/low-confidence questions held for your review",
                extra={"held_questions": held},
            )

        if self.detect_captcha(page):
            return self.escalate_now(page, "CAPTCHA before submit")

        if self.auto.get("supervised_mode", True):
            print("\n⏸  SUPERVISED MODE: review the browser window, then press Enter to submit "
                  "(or Ctrl+C to abort)...")
            try:
                input()
            except KeyboardInterrupt:
                return RunResult(status="held", reason="Aborted at supervised-mode gate")

        # The user may have clicked submit themselves during the supervised
        # gate — if we're already on the thanks page, we're done.
        if self._confirmed(page):
            return RunResult(status="submitted",
                             reason="Confirmation detected (submitted during supervised gate)")

        # First VISIBLE candidate only: Lever ships a hidden
        # button#hcaptchaSubmitBtn[type=submit] earlier in the DOM, and a
        # plain first-match selector grabs it and times out on the click.
        submit = self._visible(page, "#btn-submit, button[type='submit']")
        if not submit:
            return self.escalate_now(page, "No visible submit button found")
        try:
            submit.click()
        except Exception:
            # The click can race the post-submit navigation: Lever navigates
            # to /thanks and the button detaches while Playwright retries.
            # If we landed on the thanks page, the submission went through.
            page.wait_for_timeout(2000)
            if self._confirmed(page):
                return RunResult(status="submitted",
                                 reason="Confirmation detected (click raced navigation)")
            raise
        page.wait_for_timeout(4000)

        if self._confirmed(page):
            return RunResult(status="submitted", reason="Confirmation detected")
        error = page.query_selector(".error, [class*='error-message']")
        if error:
            return self.escalate_now(page, f"Submission error: {(error.inner_text() or '')[:200]}")
        return self.escalate_now(page, "Submission unverified — no confirmation marker found")

    @staticmethod
    def _confirmed(page) -> bool:
        try:
            content = page.content().lower()
        except Exception:
            content = ""
        return "/thanks" in page.url or "application submitted" in content \
            or ("thank you" in content and "applying" in content)
