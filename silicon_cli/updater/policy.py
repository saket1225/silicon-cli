"""Updater-side classification for legacy flat installations.

The Stemcell's eventual data-policy manifest supersedes this compatibility
list.  Until then, the updater errs toward protecting unknown local files and
never imports known living data, secrets, process state, or caches into an
immutable code generation.
"""
from __future__ import annotations

RUNTIME_EXACT = {
    ".DS_Store",
    ".env",
    ".glass.json",
    ".glass_agent.err.log",
    ".glass_agent.log",
    ".glass_agent.pid",
    ".glass_agent.pid.meta.json",
    ".glass-push.log",
    ".glass-push.pid",
    ".glass-push.pid.meta.json",
    ".silicon.err.log",
    ".silicon.log",
    ".silicon.pid",
    ".silicon.pid.meta.json",
    ".silicon.stop",
    "env.py",
    "silicon.json",
    "prompts/CONTACTS.md",
    "prompts/LORE.md",
    "prompts/MEMORY.md",
    "core/cron/checkbacks.json",
    "core/cron/history.json",
    "core/interface_state/contacts.json",
    "core/interface_state/contacts_backup.json",
    "core/interface_state/crons.json",
    "core/interface_state/manager_queue.json",
    "worker/outputs/_active_workers.json",
    "worker/outputs/_archive_meta.json",
    "worker/outputs/_browser_queue.json",
    "worker/outputs/_worker_registry.json",
}

RUNTIME_PREFIXES = {
    ".git/",
    ".home/",
    ".local/",
    ".silicon/",
    ".silicon-interface/",
    ".silicon-upstream/",
    ".tools/",
    ".venv/",
    "__pycache__/",
    "core/interface_state/media/",
    "core/activity_logs/",
    "prompts/memory/",
    "sessions/",
    "worker/outputs/",
}

CHECKPOINT_EXACT_EXCLUDES = {
    ".silicon.pid",
    ".silicon.stop",
    ".glass_agent.pid",
    ".glass-push.pid",
    ".DS_Store",
}

CHECKPOINT_PREFIX_EXCLUDES = {
    ".git/",
    ".silicon/",
    ".venv/",
    "__pycache__/",
}


def is_runtime_path(relative: str) -> bool:
    if relative in RUNTIME_EXACT or relative.endswith((".pyc", ".pyo")):
        return True
    return any(relative.startswith(prefix) for prefix in RUNTIME_PREFIXES)
