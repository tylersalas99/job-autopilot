"""Registry: ATS name → handler class. Adding an ATS = one import + one line."""
from __future__ import annotations

from handlers.ashby import AshbyHandler
from handlers.greenhouse import GreenhouseHandler
from handlers.lever import LeverHandler
from handlers.smartrecruiters import SmartRecruitersHandler
from handlers.workday import WorkdayHandler

REGISTRY = {
    "greenhouse": GreenhouseHandler,
    "lever": LeverHandler,
    "ashby": AshbyHandler,
    "workday": WorkdayHandler,
    "smartrecruiters": SmartRecruitersHandler,
}


def get_handler(ats: str):
    return REGISTRY.get(ats)
