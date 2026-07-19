"""Render tailored resume/cover letter to PDF via an HTML template.

Uses weasyprint if available; falls back to writing the HTML file (which any
browser can print to PDF) so the pipeline never hard-fails on rendering.
"""
from __future__ import annotations

import html
from pathlib import Path

RESUME_CSS = """
@page { size: letter; margin: 0.5in 0.6in; }
* { margin: 0; padding: 0; }
body { font-family: Helvetica, Arial, sans-serif; font-size: 9.6pt; color: #1a1a1a; line-height: 1.32; }
h1 { font-size: 17pt; letter-spacing: .5px; }
.contact { font-size: 8.8pt; color: #333; margin: 2px 0 6px; }
.summary { font-style: italic; margin-bottom: 8px; }
h2 { font-size: 10.5pt; text-transform: uppercase; letter-spacing: 1px;
     border-bottom: 1.2px solid #1a1a1a; margin: 9px 0 4px; padding-bottom: 1px; }
.job-head { display: flex; justify-content: space-between; margin-top: 5px; }
.job-head b { font-size: 10pt; }
.dates { color: #444; font-size: 8.8pt; }
ul { margin: 2px 0 4px 16px; }
li { margin-bottom: 2px; }
.skills p { margin-bottom: 1.5px; }
.stack { color: #444; font-size: 8.6pt; }
"""


def _esc(s: str) -> str:
    return html.escape(str(s or ""))


def resume_html(profile: dict, tailored: dict, track: str) -> str:
    ident = profile["identity"]
    parts = [
        f"<h1>{_esc(ident['full_name'])}</h1>",
        "<div class='contact'>"
        f"{_esc(ident['location'])} &nbsp;|&nbsp; {_esc(ident['phone'])} &nbsp;|&nbsp; "
        f"{_esc(ident['email'])} &nbsp;|&nbsp; {_esc(ident['linkedin'])} &nbsp;|&nbsp; "
        f"{_esc(ident['github'])}</div>",
        f"<div class='summary'>{_esc(tailored['summary'])}</div>",
        "<h2>Technical Skills</h2><div class='skills'>",
    ]
    for section, items in tailored.get("skills", {}).items():
        parts.append(f"<p><b>{_esc(section)}:</b> {_esc(', '.join(items))}</p>")
    parts.append("</div><h2>Experience</h2>")

    dates = {j["company"]: j for j in profile["work_history"]}
    for job in tailored.get("jobs", []):
        src = dates.get(job["company"], {})
        start, end = str(src.get("start", "")), str(src.get("end", ""))
        end = "Present" if end == "present" else end
        parts.append(
            f"<div class='job-head'><span><b>{_esc(job['title'])}</b> &nbsp;|&nbsp; "
            f"{_esc(job['company'])}</span><span class='dates'>{_esc(start)} – {_esc(end)}</span></div><ul>"
        )
        parts += [f"<li>{_esc(b['text'])}</li>" for b in job.get("bullets", [])]
        parts.append("</ul>")

    parts.append("<h2>Projects</h2>")
    for key in tailored.get("project_keys_ordered", []):
        proj = profile["projects"].get(key)
        if not proj:
            continue
        parts.append(
            f"<div class='job-head'><span><b>{_esc(proj['name'])}</b></span>"
            f"<span class='stack'>{_esc(', '.join(proj['stack']))}</span></div><ul>"
        )
        parts += [f"<li>{_esc(b)}</li>" for b in proj["bullets"]]
        parts.append("</ul>")

    parts.append("<h2>Education</h2>")
    for edu in profile.get("education", []):
        parts.append(
            f"<div class='job-head'><span><b>{_esc(edu['degree'])}</b> &nbsp;|&nbsp; "
            f"{_esc(edu['school'])}</span><span class='dates'>{_esc(str(edu['graduated']))}</span></div>"
        )
    body = "".join(parts)
    return f"<!doctype html><html><head><meta charset='utf-8'><style>{RESUME_CSS}</style></head><body>{body}</body></html>"


def cover_letter_html(profile: dict, letter_text: str) -> str:
    ident = profile["identity"]
    paras = "<p>Dear Hiring Manager,</p>"
    paras += "".join(f"<p>{_esc(p)}</p>" for p in letter_text.split("\n\n") if p.strip())
    paras += f"<p>Sincerely,<br>{_esc(ident['full_name'])}</p>"
    css = ("@page{size:letter;margin:1in}body{font-family:Helvetica,Arial,sans-serif;"
           "font-size:10.5pt;line-height:1.5}p{margin-bottom:10px}h1{font-size:14pt;margin-bottom:2px}"
           ".contact{font-size:9pt;color:#333;margin-bottom:18px}")
    return (f"<!doctype html><html><head><meta charset='utf-8'><style>{css}</style></head><body>"
            f"<h1>{_esc(ident['full_name'])}</h1>"
            f"<div class='contact'>{_esc(ident['email'])} | {_esc(ident['phone'])}</div>"
            f"{paras}</body></html>")


def render_pdf(html_str: str, out_path: Path, fit_one_page: bool = False) -> Path:
    """Write PDF if weasyprint is available; otherwise write .html fallback.

    fit_one_page: if the document overflows one page, progressively shrink the
    base font (down to 88%) until it fits; keeps the last attempt if it never does.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        from weasyprint import HTML  # type: ignore
        doc = HTML(string=html_str).render()
        if fit_one_page and len(doc.pages) > 1:
            for scale in (0.96, 0.93, 0.90, 0.88):
                override = (f"<style>body{{font-size:{9.6 * scale:.2f}pt;"
                            f"line-height:{1.32 * scale:.2f};}}"
                            f"h2{{margin:{9 * scale:.1f}px 0 {4 * scale:.1f}px;}}"
                            f"li{{margin-bottom:{2 * scale:.1f}px;}}</style>")
                candidate = HTML(string=html_str.replace("</head>", override + "</head>")).render()
                doc = candidate
                if len(candidate.pages) == 1:
                    break
        doc.write_pdf(str(out_path))
        return out_path
    except Exception:
        fallback = out_path.with_suffix(".html")
        fallback.write_text(html_str, encoding="utf-8")
        return fallback
