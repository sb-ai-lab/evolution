"""First-measurement quickstart: the project skeleton and the commands built on it."""
from evolution_lab.quickstart._campaign import CampaignResult, campaign
from evolution_lab.quickstart._errors import QuickstartError
from evolution_lab.quickstart._evaluate import AgentStderr, EvaluateResult, evaluate, summary_schema
from evolution_lab.quickstart._resolve import Resolution, resolve_project
from evolution_lab.quickstart._skeleton import SKELETON_FILES, new_project

__all__ = [
           "CampaignResult",
           "campaign",
           "SKELETON_FILES",
           "AgentStderr",
           "EvaluateResult",
           "QuickstartError",
           "Resolution",
           "evaluate",
           "new_project",
           "resolve_project",
           "summary_schema",
]
