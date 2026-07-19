"""Escalation: save state for human attention + notify."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path


def escalate(pending_dir: str | Path, *, reason: str, url: str, company: str = "",
             title: str = "", screenshot_bytes: bytes | None = None,
             page_html: str | None = None, extra: dict | None = None) -> Path:
    """Persist everything needed to resume manually; returns the case folder."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in (company or "unknown"))[:40]
    case = Path(pending_dir) / f"{stamp}_{safe}"
    case.mkdir(parents=True, exist_ok=True)
    (case / "case.json").write_text(json.dumps({
        "reason": reason, "url": url, "company": company, "title": title,
        "created": stamp, **(extra or {}),
    }, indent=2), encoding="utf-8")
    if screenshot_bytes:
        (case / "screenshot.png").write_bytes(screenshot_bytes)
    if page_html:
        (case / "page.html").write_text(page_html, encoding="utf-8")
    held = (extra or {}).get("held_questions")
    if held:
        # Editable template: review/edit the drafts, then re-run with
        #   python main.py <url> --answers <case>/approved_answers.json
        (case / "approved_answers.json").write_text(json.dumps(
            {q["question"]: q.get("draft_answer", "") for q in held},
            indent=2, ensure_ascii=False), encoding="utf-8")
    return case


def notify(cfg: dict, message: str) -> None:
    method = (cfg.get("notifications") or {}).get("method", "console")
    if method == "ntfy":
        topic = cfg["notifications"].get("ntfy_topic")
        if topic:
            try:
                import requests
                requests.post(f"https://ntfy.sh/{topic}", data=message.encode(), timeout=10)
                return
            except Exception:
                pass  # fall through to console
    print(f"\n🔔 {message}")
