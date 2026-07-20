"""Intake: fetch a job posting URL, follow redirects, detect the ATS."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )
}

ATS_DOMAIN_PATTERNS = [
    (r"boards\.greenhouse\.io|job-boards\.greenhouse\.io|greenhouse\.io/.*/jobs", "greenhouse"),
    (r"jobs\.lever\.co", "lever"),
    (r"myworkdayjobs\.com|workday\.com", "workday"),
    (r"jobs\.ashbyhq\.com|ashbyhq\.com", "ashby"),
    (r"smartrecruiters\.com", "smartrecruiters"),
    (r"icims\.com", "icims"),
    (r"bamboohr\.com", "bamboohr"),
    (r"linkedin\.com", "linkedin"),  # detected so we can refuse it explicitly
]

# Markers found in page HTML when a company embeds an ATS on its own domain
ATS_HTML_MARKERS = [
    ("greenhouse.io/embed", "greenhouse"),
    ("boards.greenhouse.io", "greenhouse"),
    ("job-boards.greenhouse.io", "greenhouse"),
    ("boards-api.greenhouse.io", "greenhouse"),
    ("grnh.se", "greenhouse"),        # Greenhouse's short-link domain
    ("gh_jid", "greenhouse"),         # Greenhouse job-id query param on embeds
    ("grnhse_app", "greenhouse"),     # the JS-embed mount point div id
    ("jobs.lever.co", "lever"),
    ("ashbyhq.com", "ashby"),
    ("myworkdayjobs.com", "workday"),
]

SUPPORTED_ATS = {"greenhouse", "lever", "ashby", "workday", "smartrecruiters"}
BLOCKED_ATS = {"linkedin"}      # policy: never automate LinkedIn


@dataclass
class Posting:
    url: str
    final_url: str
    ats: str                    # greenhouse | lever | ... | unknown
    html: str
    title: str = ""
    company: str = ""
    location: str = ""
    description: str = ""
    closed: bool = False        # posting no longer live (e.g. Greenhouse
                                # redirected /jobs/<id> to the board index)
    warnings: list[str] = field(default_factory=list)


def detect_ats(final_url: str, html: str) -> str:
    for pattern, name in ATS_DOMAIN_PATTERNS:
        if re.search(pattern, final_url, re.IGNORECASE):
            return name
    lowered = html.lower()
    for marker, name in ATS_HTML_MARKERS:
        if marker in lowered:
            return name
    return "unknown"


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def company_from_greenhouse_url(url: str) -> str:
    """Derive the company from a Greenhouse board URL slug as a fallback.

    https://job-boards.greenhouse.io/airtable/jobs/840... → "Airtable"
    """
    m = re.search(r"greenhouse\.io/(?:embed/job_app\?[^ ]*for=)?([a-z0-9_-]+)/jobs/",
                  url, re.IGNORECASE)
    if not m:
        m = re.search(r"greenhouse\.io/([a-z0-9_-]+)(?:/|$)", url, re.IGNORECASE)
    if not m:
        return ""
    slug = m.group(1)
    if slug in ("embed", "job_app", "boards"):
        return ""
    return re.sub(r"[-_]+", " ", slug).title()


def _extract_greenhouse(soup: BeautifulSoup, posting: Posting) -> None:
    title = soup.select_one("h1.app-title, .job__title h1, h1")
    company = soup.select_one(".company-name, [class*='company']")
    location = soup.select_one(".location, .job__location, [class*='location']")
    body = soup.select_one("#content, .job__description, [class*='description']")
    posting.title = _clean(title.get_text()) if title else ""
    posting.company = _clean(company.get_text()) if company else ""
    posting.location = _clean(location.get_text()) if location else ""
    posting.description = _clean(body.get_text(" ")) if body else ""
    if not posting.company:
        og_site = soup.find("meta", property="og:site_name")
        if og_site:
            posting.company = _clean(og_site.get("content", ""))
    if not posting.company:
        posting.company = company_from_greenhouse_url(posting.final_url)


def company_from_path_slug(url: str) -> str:
    """Lever/Ashby URLs carry the company as the first path segment:
    jobs.lever.co/<company>/<id>, jobs.ashbyhq.com/<company>/<id>."""
    path = urlparse(url).path.strip("/")
    slug = path.split("/")[0] if path else ""
    if not slug:
        return ""
    return re.sub(r"[-_]+", " ", slug).title()


def workday_api_url(url: str) -> str | None:
    """Workday's public CXS endpoint for a posting — job pages are
    JS-rendered (plain fetch sees no description), but
    /wday/cxs/<tenant>/<site>/job/<path> returns the posting as JSON.

    https://acme.wd5.myworkdayjobs.com/en-US/careers/job/City/Title_JR-1
    → https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/careers/job/City/Title_JR-1
    """
    p = urlparse(url)
    m = re.match(r"([\w-]+)\.wd\d+\.myworkdayjobs\.com$", p.netloc, re.IGNORECASE)
    if not m:
        return None
    tenant = m.group(1)
    segs = [s for s in p.path.split("/") if s]
    if segs and re.fullmatch(r"[a-z]{2}-[a-z]{2}", segs[0], re.IGNORECASE):
        segs = segs[1:]  # optional locale segment (en-US)
    # Drop a trailing /apply (or /apply/...) — the apply flow shares the job
    # path but the CXS job endpoint 404s/empties when it's kept.
    if segs and segs[-1].lower() == "apply":
        segs = segs[:-1]
    if len(segs) < 3 or segs[1] != "job":
        return None
    site, rest = segs[0], "/".join(segs[2:])
    return f"https://{p.netloc}/wday/cxs/{tenant}/{site}/job/{rest}"


def company_from_workday_url(url: str) -> str:
    """acme.wd5.myworkdayjobs.com → "Acme" (tenant slug fallback)."""
    m = re.match(r"([\w-]+)\.wd\d+\.myworkdayjobs\.com$",
                 urlparse(url).netloc, re.IGNORECASE)
    if not m:
        return ""
    return re.sub(r"[-_]+", " ", m.group(1)).title()


def _enrich_workday(posting: Posting) -> None:
    """Fill title/company/description from the CXS JSON. Fail-soft: any
    problem just leaves the (thin) generic extraction in place."""
    api = workday_api_url(posting.final_url)
    if not api:
        return
    try:
        resp = requests.get(api, headers={**HEADERS, "Accept": "application/json"},
                            timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        posting.warnings.append(
            "Workday CXS lookup failed — description may be thin.")
        return
    info = data.get("jobPostingInfo") or {}
    if info.get("title"):
        posting.title = _clean(info["title"])
    desc = info.get("jobDescription") or ""
    if desc:
        posting.description = _clean(
            BeautifulSoup(desc, "html.parser").get_text(" "))
    if info.get("location"):
        posting.location = _clean(str(info["location"]))
    org = (data.get("hiringOrganization") or {}).get("name")
    if org:
        posting.company = _clean(org)


def smartrecruiters_ids(url: str) -> tuple[str, str] | None:
    """(companyIdentifier, postingId) from a SmartRecruiters posting URL.

    https://jobs.smartrecruiters.com/Visa/744000133907678-sr-manager
    → ("Visa", "744000133907678")
    Careers-site URLs (careers.smartrecruiters.com/<Company>) carry no
    posting id — those return None.
    """
    p = urlparse(url)
    if not re.search(r"(^|\.)smartrecruiters\.com$", p.netloc, re.IGNORECASE):
        return None
    m = re.match(r"/([^/]+)/(\d+)(?:-|$)", p.path)
    if not m:
        return None
    return m.group(1), m.group(2)


def smartrecruiters_api_url(url: str) -> str | None:
    """Public posting API — no auth needed. Returns title/company/location,
    the full job ad, active-ness, AND the publication `uuid` that the apply
    form URL is built from."""
    ids = smartrecruiters_ids(url)
    if not ids:
        return None
    company, posting_id = ids
    return f"https://api.smartrecruiters.com/v1/companies/{company}/postings/{posting_id}"


def smartrecruiters_apply_url(company_identifier: str, publication_uuid: str) -> str:
    """The oneclick-ui "Easy Apply" form URL. The posting API's `uuid` IS
    the publication UUID (verified live against Visa 2026-07-19), so the
    handler never has to find and click "I'm interested"."""
    return (f"https://jobs.smartrecruiters.com/oneclick-ui/company/"
            f"{company_identifier}/publication/{publication_uuid}"
            f"?dcr_ci={company_identifier}")


def _enrich_smartrecruiters(posting: Posting) -> None:
    """Fill title/company/location/description from the public posting API
    and rewrite final_url to the oneclick-ui apply form. Fail-soft: on any
    problem the job-page URL stays and the handler falls back to clicking
    "I'm interested"."""
    api = smartrecruiters_api_url(posting.final_url) \
        or smartrecruiters_api_url(posting.url)
    if not api:
        return
    try:
        resp = requests.get(api, headers={**HEADERS, "Accept": "application/json"},
                            timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        posting.warnings.append(
            "SmartRecruiters API lookup failed — the handler will click "
            "\"I'm interested\" to reach the form.")
        return
    if data.get("name"):
        posting.title = _clean(data["name"])
    org = (data.get("company") or {}).get("name")
    if org:
        posting.company = _clean(org)
    loc = (data.get("location") or {}).get("fullLocation")
    if loc:
        posting.location = _clean(str(loc))
    sections = (data.get("jobAd") or {}).get("sections") or {}
    parts = []
    for sec in sections.values():
        if isinstance(sec, dict) and sec.get("text"):
            parts.append(BeautifulSoup(sec["text"], "html.parser").get_text(" "))
    if parts:
        posting.description = _clean(" ".join(parts))
    if data.get("active") is False:
        posting.closed = True
        posting.warnings.append(
            "Posting appears CLOSED — SmartRecruiters API reports it inactive.")
    ids = smartrecruiters_ids(posting.final_url) or smartrecruiters_ids(posting.url)
    if ids and data.get("uuid"):
        posting.final_url = smartrecruiters_apply_url(ids[0], data["uuid"])


def _extract_generic(soup: BeautifulSoup, posting: Posting) -> None:
    title = soup.find("h1") or soup.find("title")
    posting.title = _clean(title.get_text()) if title else ""
    og_site = soup.find("meta", property="og:site_name")
    if og_site:
        posting.company = _clean(og_site.get("content", ""))
    for tag in soup(["script", "style", "nav", "header", "footer"]):
        tag.decompose()
    posting.description = _clean(soup.get_text(" "))[:20000]


def fetch_posting(url: str, timeout: int = 30) -> Posting:
    """Fetch the posting and extract structured fields. Raises on network error."""
    resp = requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
    resp.raise_for_status()
    html = resp.text
    final_url = str(resp.url)
    ats = detect_ats(final_url, html)
    posting = Posting(url=url, final_url=final_url, ats=ats, html=html)

    # Greenhouse signals a dead posting by redirecting /jobs/<id> to the
    # board index with ?error=true — catch it here, before any tailoring.
    # (A redirect to a NON-greenhouse host is the embedded-board case
    # handled below, not a closed posting.)
    if ats == "greenhouse" and "/jobs/" in url and (
            "error=true" in final_url
            or ("greenhouse.io" in urlparse(final_url).netloc
                and "/jobs/" not in final_url)):
        posting.closed = True
        posting.warnings.append(
            "Posting appears CLOSED — Greenhouse redirected to the board index.")

    # Embedded Greenhouse board: some companies (e.g. Samsara) redirect
    # boards.greenhouse.io/<slug>/jobs/<id> to their own careers page, where
    # the form lives in a JS-injected iframe (div#grnhse_app) the handler
    # can't reach. Point the handler at Greenhouse's direct embed-form URL.
    if ats == "greenhouse" and "greenhouse.io" not in urlparse(final_url).netloc:
        jid = (re.search(r"[?&]gh_jid=(\d+)", final_url)
               or re.search(r"/jobs/(\d+)", url))
        slug_m = (re.search(r"boards\.greenhouse\.io/([\w-]+)/jobs?", url)
                  or re.search(r"greenhouse\.io/embed/job_board(?:/js)?\?for=([\w-]+)", html))
        if jid and slug_m and slug_m.group(1) not in ("embed", "job_app", "boards"):
            posting.final_url = ("https://job-boards.greenhouse.io/embed/job_app"
                                 f"?for={slug_m.group(1)}&token={jid.group(1)}")
            posting.warnings.append(
                "Embedded Greenhouse board — redirecting the browser to the "
                "direct application form URL.")

    soup = BeautifulSoup(html, "html.parser")
    if ats == "greenhouse":
        _extract_greenhouse(soup, posting)
    else:
        _extract_generic(soup, posting)
        if ats in ("lever", "ashby") and not posting.company:
            posting.company = company_from_path_slug(final_url)
        if posting.company:
            # Lever <title> is "Company - Job Title"; Ashby uses "Job Title @ Company".
            prefix = posting.company.lower() + " - "
            if posting.title.lower().startswith(prefix):
                posting.title = posting.title[len(prefix):].strip()
            posting.title = re.sub(
                rf"\s*@\s*{re.escape(posting.company)}.*$", "", posting.title,
                flags=re.IGNORECASE).strip()
        if ats == "workday":
            if not posting.company:
                posting.company = company_from_workday_url(final_url)
            _enrich_workday(posting)
        if ats == "smartrecruiters":
            if not posting.company:
                posting.company = company_from_path_slug(final_url)
            _enrich_smartrecruiters(posting)
    if not posting.description or len(posting.description) < 200:
        posting.warnings.append(
            "Job description extraction looks thin — page may be JS-rendered; "
            "the browser handler will re-extract it."
        )
    if ats in BLOCKED_ATS:
        posting.warnings.append("LinkedIn postings are excluded by policy — find the company's direct posting.")
    elif ats == "unknown":
        posting.warnings.append("Unrecognized ATS — will escalate rather than guess.")
    elif ats not in SUPPORTED_ATS:
        posting.warnings.append(f"ATS '{ats}' detected but no handler exists yet — will escalate.")
    return posting


def check_exclusions(posting: Posting, rules: dict) -> str | None:
    """Return a rejection reason, or None if the posting passes all filters."""
    company_l = posting.company.lower()
    for blocked in rules.get("companies_blocklist") or []:
        if blocked.lower() in company_l:
            return f"Company '{posting.company}' is on your blocklist"
    must_match = rules.get("title_must_match_any") or []
    if must_match and not any(m.lower() in posting.title.lower() for m in must_match):
        return f"Title '{posting.title}' matches none of your allowed patterns"
    text_l = (posting.title + " " + posting.description).lower()
    for kw in rules.get("keywords_reject") or []:
        if kw.lower() in text_l:
            return f"Posting contains rejected keyword: '{kw}'"
    return None
