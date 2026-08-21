"""Project slug extraction. Pure function, no I/O."""

from __future__ import annotations

import re

# Strip a leading "YYYY_" date prefix (4 digits + underscore) from a
# workspace directory's last path component. Only matches 4-digit year,
# not "23_" or "2024_anything".
_DATE_PREFIX_RE = re.compile(r"^\d{4}_")


def project_from_workspace(workspace_dir: str | None) -> str | None:
    """Extract a project slug from a workspace directory path.

    Rules (matching legacy `build_project_dashboard.py`):
      - None or empty → None
      - last path component is the base name
      - leading `YYYY_` prefix is stripped ("0803_my-project" → "my-project")
      - directories containing "agent_meta" → None (agent's own workspace)
    """
    if not workspace_dir:
        return None
    normalized = workspace_dir.replace("\\", "/").rstrip("/")
    if "agent_meta" in normalized.lower():
        return None
    parts = normalized.split("/")
    if not parts or not parts[-1]:
        return None
    name = _DATE_PREFIX_RE.sub("", parts[-1])
    return name or None
