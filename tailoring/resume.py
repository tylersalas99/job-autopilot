"""Tailoring: classify the JD, tailor resume content, validate against the
profile store (anti-fabrication), and draft a cover letter.

All Claude calls read ANTHROPIC_API_KEY from the environment.
"""
from __future__ import annotations

import json
import os
import random
import re
import time

import anthropic

_client: anthropic.Anthropic | None = None


def client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. Get a key at https://console.anthropic.com "
                "and export it before running."
            )
        # SDK retries off — _create owns retry/backoff so behavior is
        # deterministic and a sustained 429/529 doesn't kill a run.
        _client = anthropic.Anthropic(max_retries=0)
    return _client


# Transient failures worth retrying: rate limits (429), server errors
# (5xx, incl. 529 overloaded), and connection drops/timeouts. Everything
# else (auth, bad request, ...) fails fast — retrying can't fix it.
_RETRYABLE = (
    anthropic.APIConnectionError,
    anthropic.RateLimitError,
    anthropic.InternalServerError,
)


def _retry_after_seconds(exc: Exception) -> float | None:
    """Honor the server's Retry-After header when present (capped)."""
    resp = getattr(exc, "response", None)
    val = resp.headers.get("retry-after") if resp is not None else None
    try:
        return min(float(val), 120.0) if val else None
    except (TypeError, ValueError):
        return None


def _create(cfg: dict, **kwargs) -> anthropic.types.Message:
    """messages.create with exponential backoff + jitter on transient errors.

    One overloaded moment mid-run must not kill a half-filled application.
    Attempts = anthropic.max_retries (config.yaml, default 5) + 1.
    """
    retries = int(cfg.get("anthropic", {}).get("max_retries", 5))
    delay = 2.0
    for attempt in range(1, retries + 2):
        try:
            return client().messages.create(**kwargs)
        except _RETRYABLE as exc:
            if attempt > retries:
                raise
            wait = _retry_after_seconds(exc)
            if wait is None:
                wait = min(delay, 60.0) * (0.5 + random.random())  # jitter
                delay *= 2
            print(f"  ⚠ API {type(exc).__name__} — retry {attempt}/{retries} "
                  f"in {wait:.0f}s")
            time.sleep(wait)
    raise RuntimeError("unreachable")  # pragma: no cover


def _ask_json(cfg: dict, system: str, user: str) -> dict:
    """Call Claude expecting a bare-JSON reply; strip fences defensively."""
    resp = _create(
        cfg,
        model=cfg["anthropic"]["model"],
        max_tokens=cfg["anthropic"]["max_tokens"],
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    text = "".join(b.text for b in resp.content if b.type == "text")
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Model wrapped the JSON in prose — extract the outermost object.
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            return json.loads(text[start:end + 1])
        raise


def classify_track(cfg: dict, profile: dict, jd_text: str, title: str) -> str:
    """Pick which resume track (swe / dataeng / ...) fits this JD."""
    tracks = {k: v["label"] for k, v in profile["tracks"].items()}
    result = _ask_json(
        cfg,
        system=(
            "You classify job descriptions. Respond ONLY with JSON: "
            '{"track": "<key>", "reason": "<one sentence>"} — no other text.'
        ),
        user=(
            f"Available tracks: {json.dumps(tracks)}\n\n"
            f"Job title: {title}\n\nJob description:\n{jd_text[:6000]}\n\n"
            "Which track key best fits this role?"
        ),
    )
    track = result.get("track")
    return track if track in profile["tracks"] else next(iter(profile["tracks"]))


def sanitize_skills(tailored: dict, source_skills: dict) -> None:
    """Deterministically remove invented skills — every rendered skill item must
    exist verbatim in the source profile.

    Claude may reorder sections/items and drop irrelevant ones; it may NOT add
    or embellish. Exact (case/space-insensitive) matches are kept; embellished
    items ("Pytest (unit + integration)") are mapped back to their source item
    ("Pytest"); everything else is dropped. Emptied sections disappear. If
    nothing survives, fall back to the source skills unchanged.
    """
    def canon(s: str) -> str:
        return re.sub(r"\s+", " ", str(s).lower()).strip()

    allowed = {canon(item): str(item)
               for items in source_skills.values() for item in items}
    out: dict = {}
    for section, items in (tailored.get("skills") or {}).items():
        kept = []
        for item in items or []:
            c = canon(item)
            pick = allowed.get(c)
            if pick is None:  # embellished? map back to the contained source item
                pick = next((orig for ca, orig in allowed.items()
                             if ca in c or c in ca), None)
            if pick and pick not in kept:
                kept.append(pick)
        if kept:
            out[section] = kept
    tailored["skills"] = out or {k: list(v) for k, v in source_skills.items()}


def tailor_resume(cfg: dict, profile: dict, track: str, jd_text: str, title: str,
                  company: str, feedback: list[str] | None = None) -> dict:
    """Return tailored resume content: summary, ordered bullets per job, skills, projects.

    Claude may reorder bullets and lightly rephrase to mirror JD language, but
    every bullet must trace back to a bullet id in the profile store.
    `feedback`: fact-check problems from a previous attempt, for a retry.
    """
    track_data = profile["tracks"][track]
    source = {
        "summary": track_data["summary"],
        "skills": track_data["skills"],
        "work_history": profile["work_history"],
        "projects": {k: profile["projects"][k] for k in track_data["project_order"]},
    }
    system = (
        "You tailor resumes. HARD RULES: (1) Never invent skills, tools, metrics, "
        "employers, titles, or accomplishments not present in the source profile. "
        "(2) You may reorder bullets and lightly rephrase to mirror the job "
        "description's terminology, but each bullet must keep its source 'id' and "
        "preserve its factual claims and numbers exactly. (3) Skills must be copied "
        "VERBATIM from the source skills — reorder or omit items, never add, merge, "
        "or embellish them. (4) The summary may only rephrase the source summary; "
        "do not introduce practices, tools, or claims it does not contain. "
        "(5) Respond ONLY with JSON:\n"
        '{"summary": str, "jobs": [{"company": str, "title": str, "bullets": '
        '[{"id": str, "text": str}]}], "skills": {section: [str]}, '
        '"project_keys_ordered": [str]}'
    )
    user = (
        f"Target role: {title} at {company}\n\nJob description:\n{jd_text[:8000]}\n\n"
        f"Source profile (the ONLY permitted facts):\n{json.dumps(source, default=str)}"
    )
    if feedback:
        user += (
            "\n\nIMPORTANT: a previous attempt FAILED fact-checking with the problems "
            "below. Produce a corrected version that avoids every one of them — when "
            "in doubt, stay closer to the source wording:\n- " + "\n- ".join(feedback)
        )
    tailored = _ask_json(cfg, system, user)
    sanitize_skills(tailored, track_data["skills"])
    return tailored


def validate_tailored(cfg: dict, profile: dict, tailored: dict) -> list[str]:
    """Second pass: flag any tailored claim not grounded in the profile store.

    Returns a list of problems; empty list = safe to use. Also runs cheap
    deterministic checks (bullet ids and numeric claims) before asking Claude.
    """
    problems: list[str] = []

    valid_ids = {b["id"] for job in profile["work_history"] for b in job["bullets"]}
    source_bullets = {
        b["id"]: b["text"] for job in profile["work_history"] for b in job["bullets"]
    }
    for job in tailored.get("jobs", []):
        for b in job.get("bullets", []):
            if b.get("id") not in valid_ids:
                problems.append(f"Bullet id '{b.get('id')}' does not exist in profile store")
                continue
            src_nums = set(re.findall(r"\d+(?:\.\d+)?%?", source_bullets[b["id"]]))
            new_nums = set(re.findall(r"\d+(?:\.\d+)?%?", b.get("text", "")))
            invented = new_nums - src_nums
            if invented:
                problems.append(
                    f"Bullet '{b['id']}' introduces numbers not in source: {sorted(invented)}"
                )
    if problems:
        return problems  # don't spend an API call on something already broken

    result = _ask_json(
        cfg,
        system=(
            "You are a strict fact-checker. Compare a tailored resume against the "
            "source profile (work-history bullets, skills lists, and track summaries). "
            "Flag ONLY claims in the tailored resume that appear NOWHERE in the source "
            "profile. Anything stated anywhere in the source — including its summary "
            "text and skills lists — is grounded BY DEFINITION: never flag source "
            "content for lacking additional support elsewhere in the profile, and "
            "never fact-check the source against itself. Rephrasings that preserve "
            "the source's meaning are fine; only inventions and embellishments are "
            "problems. Respond ONLY with "
            'JSON: {"problems": ["<description>", ...]} — empty list if fully grounded.'
        ),
        user=(
            f"Source profile:\n{json.dumps(profile['work_history'], default=str)}\n"
            f"{json.dumps(profile['tracks'], default=str)}\n\n"
            f"Tailored resume:\n{json.dumps(tailored)}"
        ),
    )
    return result.get("problems", [])


def draft_cover_letter(cfg: dict, profile: dict, track: str, jd_text: str,
                       title: str, company: str) -> str:
    text = _draft_letter_once(cfg, profile, track, jd_text, title, company)
    issues = []
    if sounds_third_person(profile, text):
        # The candidate is the author — a letter about "Tyler" / "he" is wrong.
        issues.append(
            "referred to the candidate by name or in the third person. The "
            "candidate is the AUTHOR of this letter — rewrite entirely in first "
            "person ('I built', 'my work') with no third-person references")
    if sounds_like_meta_reference(text, company):
        # Echoing the JD input ("the work <Company> describes", "as described
        # in the posting") reveals the letter was machine-generated from the
        # posting (user 2026-07-21). Name the position plainly instead.
        issues.append(
            f"referenced the job posting or description as a source (e.g. 'the "
            f"work {company} describes', 'as described in the posting', 'the role "
            f"mentions'). State the candidate's own experience directly and name "
            f"the position plainly — never describe the posting or say what it "
            f"'describes', 'mentions', 'lists', or 'is looking for'")
    if issues:
        text = _draft_letter_once(
            cfg, profile, track, jd_text, title, company,
            feedback=("Your previous draft " + "; and it ".join(issues)
                      + ". Rewrite the letter fixing this:\n" + text))
    return text


def sounds_third_person(profile: dict, text: str) -> bool:
    """True if the letter body talks ABOUT the candidate instead of AS them."""
    t = text.lower()
    ident = profile["identity"]
    if ident["first_name"].lower() in t or ident["last_name"].lower() in t:
        return True
    return bool(re.search(r"\b(?:he|him|his|she|her|hers)\b", t))


_META_VOICE_RE = re.compile(
    r"\b(?:my|the|their)\s+profile\b|profile data|\bthe candidate\b|"
    r"provided (?:data|information|profile)", re.IGNORECASE)


def sounds_wrong_voice(profile: dict, text: str) -> bool:
    """True when a form answer can't be pasted VERBATIM as the candidate:
    third person ("Tyler's profile is that of...", "he"), or leaked
    application mechanics ("listed in my profile", "the candidate") — the
    hiring team reads these answers as if the candidate typed them
    (user 2026-07-14, Ashby Renewals review session)."""
    return sounds_third_person(profile, text) or bool(_META_VOICE_RE.search(text))


# Describing/soliciting verbs a letter uses when it echoes its JD input
# instead of stating the candidate's own experience.
_META_DESCRIBE_VERBS = (
    r"describe[sd]?|outlin\w+|detail[sed]*|mention[sed]*|list[sed]*|"
    r"specif\w+|note[sd]?|seek[s]?|want[s]?|require[s]?|ask[s]?|"
    r"call[s]?\s+for|is\s+looking\s+for|are\s+looking\s+for")

# Generic references to the posting/description/role as a SOURCE.
_JD_REFERENCE_RE = re.compile(
    # "as described/listed/outlined in the job/role/posting/description"
    r"\b(?:as\s+)?(?:" + _META_DESCRIBE_VERBS + r")\s+(?:in|on|by|under|within)\s+"
    r"(?:the|your|this)\s+(?:job|role|position|posting|listing|description|ad|"
    r"advertisement|opening|req|write-?up)\b"
    # "the/your job posting / listing / advertisement / job description"
    r"|\b(?:the|your|this)\s+(?:job\s+|role\s+|position\s+)?"
    r"(?:posting|listing|advertisement|write-?up|job\s+description)\b"
    # "the role/posting/description/team/company describes|mentions|seeks|…"
    r"|\b(?:the\s+)?(?:role|position|posting|listing|description|job|team|"
    r"company|opening)\s+(?:" + _META_DESCRIBE_VERBS + r")\b",
    re.IGNORECASE)

# Company-name suffixes/filler dropped before proximity-matching a describing
# verb, so "Motorola Solutions describes" is caught via the "Motorola" token.
_COMPANY_STOPWORDS = {
    "inc", "llc", "ltd", "corp", "corporation", "co", "the", "company",
    "solutions", "group", "technologies", "technology", "systems", "services",
    "global", "holdings", "labs", "studios", "studio", "and"}


def sounds_like_meta_reference(text: str, company: str = "") -> bool:
    """True when the letter references the job posting/description/role as a
    SOURCE ('the role describes', 'as described in the posting', 'the work
    <Company> describes') — a tell that the model is echoing its JD input
    rather than writing as a candidate. (user 2026-07-21: the Motorola cover
    letter leaked 'the data migration work Motorola Solutions describes'.)
    A candidate names the position plainly and states their own experience;
    they never say what the posting 'describes', 'mentions', or 'lists'."""
    if _JD_REFERENCE_RE.search(text):
        return True
    for tok in re.findall(r"[A-Za-z]{3,}", company or ""):
        if tok.lower() in _COMPANY_STOPWORDS:
            continue
        # "<CompanyToken> [up to 3 words] describes/mentions/seeks/…"
        if re.search(re.escape(tok) + r"\W+(?:\w+\W+){0,3}?(?:"
                     + _META_DESCRIBE_VERBS + r")\b", text, re.IGNORECASE):
            return True
    return False


def _draft_letter_once(cfg: dict, profile: dict, track: str, jd_text: str, title: str,
                       company: str, feedback: str | None = None) -> str:
    resp = _create(
        cfg,
        model=cfg["anthropic"]["model"],
        max_tokens=1200,
        system=(
            "Write a professional cover letter that sounds like a strong candidate "
            "wrote it, not an AI. Two or three short paragraphs, under 180 words, "
            "plain text only. Be concise — every sentence earns its place; cut "
            "throat-clearing, filler, and any restating of the obvious.\n\n"
            "VOICE RULES — all of them:\n"
            "- FIRST PERSON, always: the candidate is the author. 'I built', 'my "
            "work'. NEVER refer to the candidate by name or in the third person "
            "('Tyler built', 'he', 'his') — that reads as a letter about them, "
            "not from them.\n"
            "- Formal but natural: polished business prose, the register of a "
            "well-written application letter. Not chatty, not stiff. Complete "
            "sentences, measured tone, no slang or breezy asides.\n"
            "- Ground every claim in the provided profile — never invent experience.\n"
            "- Confidence comes from specifics, not adjectives. Pick the 1-2 things in "
            "the candidate's background that genuinely match this job and present "
            "them concretely. Skip everything else.\n"
            "- Write only from strength: state what the candidate HAS done and how it "
            "applies. NEVER mention, imply, or apologize for missing experience, "
            "gaps, or unfamiliarity — no 'want to grow into', 'ready to learn', "
            "'while I haven't', 'new to', or any aspirational framing that concedes "
            "a shortfall.\n"
            "- Do NOT parrot the job description's marketing language back at it.\n"
            "- NEVER reference the job posting or description as a source. Do not "
            "write 'the role describes', 'the work X describes', 'as described in "
            "the posting', or say what the company/role 'mentions', 'lists', "
            "'outlines', or 'is looking for'. State your own experience directly "
            "and name the position plainly (e.g. 'this Data Conversion role').\n"
            "- At most one em dash in the whole letter. No semicolons.\n"
            "- Banned phrases and patterns: 'I am writing to express', 'passionate', "
            "'excited to', 'leverage', 'aligns with', 'exactly the kind of', "
            "'is the foundation I'd bring', 'isn't new territory', 'take on and push "
            "forward', 'sits at the center of', 'X paired with Y under Z', "
            "'makes that story legible', any 'That combination —' construction, and "
            "any sentence that sounds like a LinkedIn post.\n"
            "- Close with one courteous, plain sentence expressing interest in "
            "discussing the role. Frame it personally — how *I* would fit into or "
            "contribute to the role (e.g. 'I would welcome the opportunity to "
            "discuss how I would fit into this role') — never impersonally about "
            "'this background' or 'my experience' fitting. No grand statements.\n"
            "- Output the letter BODY only: no greeting line, no sign-off, no name, "
            "no contact details. The document template adds the candidate's header "
            "and 'Dear Hiring Manager,' itself — writing them again duplicates "
            "them. Never use placeholders or brackets of any kind."
        ),
        messages=[{
            "role": "user",
            "content": (
                f"Candidate name: {profile['identity']['full_name']}\n\n"
                f"Candidate profile:\n{json.dumps(profile['work_history'], default=str)}\n"
                f"Projects:\n{json.dumps(profile.get('projects', {}), default=str)}\n"
                f"Summary: {profile['tracks'][track]['summary']}\n\n"
                f"Role: {title} at {company}\n\nJob description:\n{jd_text[:6000]}"
                + (f"\n\nIMPORTANT — {feedback}" if feedback else "")
            ),
        }],
    )
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    # Safety net: a name placeholder must never reach a real letter.
    text = re.sub(r"\[\s*(?:candidate\s*|your\s*|full\s*)*name\s*\]",
                  profile["identity"]["full_name"], text, flags=re.IGNORECASE)
    return strip_greeting_signoff(strip_preamble(text))


# Horizontal-rule fence ("---", "***", "___") the model uses to separate its
# preamble from the letter. A real letter never opens with one.
_HRULE_RE = re.compile(r"^\s*[-*_]{3,}\s*$")
# A leading line that is model chatter / an instruction restatement, not the
# letter itself.
_PREAMBLE_LINE_RE = re.compile(
    r"here(?:'s| is| are)\b[^.\n]*\bletter\b"              # "here is the letter:"
    r"|^\s*(?:sure|certainly|absolutely|of course|okay|ok)\b"  # "Sure, ..."
    r"|\bplain[\s-]+(?:body[\s-]+)?text\b"                 # "plain body text"
    r"|\bunder\s+\d+\s+words\b"                            # "under 220 words"
    r"|\b(?:one|two|three|four|\d+)\s+paragraphs?\b",      # "Three paragraphs"
    re.IGNORECASE)


def strip_preamble(text: str) -> str:
    """Drop any model preamble before the letter body — an instruction
    restatement and/or a 'here is the letter:' lead-in, optionally fenced off
    with a '---' rule. (Sony run 2026-07-21 rendered 'Three paragraphs, under
    220 words, plain body text — here is the letter:\\n---' straight into the
    PDF under 'Dear Hiring Manager,'.) A real letter has no horizontal rule or
    format-preamble at the top, so this only ever removes leaked scaffolding."""
    lines = text.split("\n")
    nonempty = [i for i, l in enumerate(lines) if l.strip()]
    # 1) An early horizontal rule fences the preamble off — drop through it,
    #    but ONLY when every line before the rule is itself preamble (so a
    #    stray rule inside real body prose can never eat the letter).
    for i in nonempty[:3]:
        if _HRULE_RE.match(lines[i]):
            before = [lines[j] for j in nonempty if j < i]
            if all(_PREAMBLE_LINE_RE.search(b) for b in before):
                lines = lines[i + 1:]
            break
    # 2) Peel any leading preamble/rule lines the model left unfenced.
    while lines:
        first = lines[0].strip()
        if not first:
            lines.pop(0); continue
        if _HRULE_RE.match(lines[0]) or _PREAMBLE_LINE_RE.search(first):
            lines.pop(0); continue
        break
    return "\n".join(lines).strip()


_GREETING_RE = re.compile(r"^(?:dear|hello|hi|greetings)\b[^\n]*\n+", re.IGNORECASE)
_SIGNOFF_RE = re.compile(
    r"\n+\s*(?:sincerely|best regards|kind regards|warm regards|regards|"
    r"respectfully|best)[,.]?\s*\n?\s*[A-Za-z .'-]{0,60}\s*$", re.IGNORECASE)


def strip_greeting_signoff(text: str) -> str:
    """The PDF template supplies the header and the single 'Dear Hiring
    Manager,' — strip any greeting/sign-off the model emitted anyway, so the
    letter can never contain duplicates."""
    text = _GREETING_RE.sub("", text.strip())
    return _SIGNOFF_RE.sub("", text).strip()


def draft_choice_answer(cfg: dict, profile: dict, question: str, options: list[str],
                        jd_text: str, title: str, company: str) -> dict:
    """Pick one option of a multiple-choice form question.

    Returns {"choice": str, "confidence": "high"|"low"}. The handler holds
    (escalates) low-confidence picks and anything that doesn't match an option.
    """
    return _ask_json(
        cfg,
        system=(
            "You answer multiple-choice job application questions on behalf of a "
            "candidate, using ONLY their profile data. Pick exactly one of the "
            "provided options and return it VERBATIM. If the profile does not "
            'clearly determine the answer, set confidence "low". Respond ONLY '
            'with JSON: {"choice": str, "confidence": "high"|"low"}'
        ),
        user=(
            f"Question: {question}\n\nOptions:\n{json.dumps(options)}\n\n"
            f"Role: {title} at {company}\n\n"
            f"Candidate profile:\n{json.dumps(profile, default=str)[:8000]}\n\n"
            f"Job description excerpt:\n{jd_text[:2000]}"
        ),
    )


def draft_field_answer(cfg: dict, profile: dict, question: str, jd_text: str,
                       title: str, company: str, avoid: str | None = None) -> dict:
    """Draft an answer for an application form question.

    Returns {"answer": str, "confidence": "high"|"low", "is_essay": bool}.
    Low-confidence or essay answers get held/escalated by the handler.
    `avoid`: a previous draft the user rejected — produce a different take.
    """
    user = (
        f"Question: {question}\n\nRole: {title} at {company}\n\n"
        f"Candidate profile:\n{json.dumps(profile, default=str)[:8000]}\n\n"
        f"Job description excerpt:\n{jd_text[:3000]}"
    )
    if avoid:
        user += (
            "\n\nThe candidate rejected the previous draft below. Write a "
            "substantially different alternative — different angle, structure, "
            "and emphasis — still grounded ONLY in the profile facts:\n" + avoid
        )
    draft = _ask_json(cfg, system=_FIELD_ANSWER_SYSTEM, user=user)
    answer = str(draft.get("answer") or "")
    # Voice guard (mirrors the cover-letter third-person redraft): the
    # answer is pasted into the form VERBATIM, so "Tyler's profile is..."
    # or "listed in my profile" must never survive. One redraft; if the
    # voice still isn't fixed, force low confidence so a human reviews it.
    if answer and sounds_wrong_voice(profile, answer):
        retry = _ask_json(
            cfg, system=_FIELD_ANSWER_SYSTEM,
            user=user + (
                "\n\nIMPORTANT — your previous draft (below) broke the voice "
                "rules: it referred to the candidate in the third person or "
                "mentioned the profile/application mechanics. Rewrite it "
                "entirely in first person AS the candidate, with no mention "
                "of profiles, resumes, or data sources:\n" + answer
            ),
        )
        if str(retry.get("answer") or "") \
                and not sounds_wrong_voice(profile, str(retry["answer"])):
            return retry
        draft["confidence"] = "low"
    return draft


_FIELD_ANSWER_SYSTEM = (
    "You answer job application form questions on behalf of a candidate, "
    "using ONLY their profile data.\n\n"
    "VOICE RULES — the hiring team reads your answer VERBATIM, as if the "
    "candidate typed it into the form:\n"
    "- FIRST PERSON, always: 'I', 'my'. NEVER the candidate's name, never "
    "'he'/'his', never 'the candidate'.\n"
    "- NEVER mention the profile, resume, or provided data ('listed in my "
    "profile', 'according to the profile', 'based on the information "
    "provided') — the reader has no idea what that means.\n"
    "- Never invent experience. When the data doesn't support a direct "
    "answer, write the closest TRUE first-person answer (adjacent "
    "experience, honest framing without dwelling on the gap) and set "
    'confidence "low" — low-confidence drafts are reviewed by the '
    "candidate before anything is sent, so a weak-but-true draft beats "
    "meta-commentary about missing information.\n\n"
    "If the question is open-ended/essay-style set is_essay true. If the "
    'profile lacks the needed info, set confidence "low". Respond ONLY '
    'with JSON: {"answer": str, "confidence": "high"|"low", "is_essay": bool}'
)
