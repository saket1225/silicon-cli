"""Glass sync — pull a silicon from Glass and run backups."""
from __future__ import annotations

import json
import os
import shutil
import ssl
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import docker_runtime, process, registry, stemcell, ui
from .config import GLASS_CLI_REPO, GLASS_SERVER_URL


@dataclass
class PullOpts:
    """Non-interactive answers for ``silicon pull``, so Glass's setup agent can
    drive it headlessly over SSH. Any field left at its default falls back to the
    old interactive behaviour (or the non-TTY default when not on a terminal)."""

    assume_yes: bool = False        # take every prompt's default without asking
    brain: str | None = None        # "claude" | "codex" | "both"
    brain_order: list[str] = field(default_factory=list)
    backup: bool | None = None      # None = ask/default; True/False = force
    name: str | None = None         # instance name for a single-silicon pull

    def setup_config_kwargs(self) -> dict:
        """Brain overrides passed to ``stemcell.choose_setup_config``."""
        order = list(self.brain_order)
        brain = self.brain
        if brain == "both" and not order:
            order = ["claude", "codex"]
            brain = "claude"
        if brain in ("claude", "codex") and not order:
            order = [brain]
        return {"brain": brain if brain in ("claude", "codex") else None,
                "brain_order": order or None}

MANIFEST_NAME = ".backupsilicon"
BACKUP_UPLOAD_PATH = "/api/v1/silicon-backups/"
BACKUP_HOUR_UTC = 23
BACKUP_MINUTE_UTC = 59
PROVIDER_API_KEYS = (
    ("GEMINI_API_KEY", "Gemini"),
    ("OPENAI_API_KEY", "OpenAI"),
    ("ELEVENLABS_API_KEY", "ElevenLabs"),
    ("DEEPGRAM_API_KEY", "Deepgram"),
    ("STEEL_API_KEY", "Steel"),
)


def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _urlopen(req, *, timeout: int | None = None):
    return urllib.request.urlopen(req, timeout=timeout, context=_ssl_context())


def _has_glass() -> bool:
    return bool(shutil.which("glass"))


def ensure_glass_cli() -> None:
    if _has_glass():
        return
    ui.info("glass CLI not found. Installing it...")
    install_glass_cli()
    if not _has_glass():
        ui.error("glass CLI installation failed")
        sys.exit(1)


def install_glass_cli() -> None:
    glass_dir = Path.home() / ".glass"
    bin_dir = Path.home() / ".local" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    archive = f"https://codeload.github.com/{GLASS_CLI_REPO}/tar.gz/refs/heads/main"
    tmp = tempfile.mkdtemp(prefix="glass-install-")
    try:
        ui.info("Installing glass CLI...")
        tarball = Path(tmp) / "glass.tar.gz"
        with _urlopen(archive, timeout=60) as resp, tarball.open("wb") as f:
            shutil.copyfileobj(resp, f)
        with tarfile.open(tarball) as tf:
            tf.extractall(tmp)
        src = next((p for p in Path(tmp).iterdir() if p.is_dir() and p.name.startswith("glass-")), None)
        if not src or not (src / "glass").exists():
            ui.warn("Could not auto-install glass CLI. Downloaded archive was invalid.")
            return
        shutil.rmtree(glass_dir, ignore_errors=True)
        glass_dir.mkdir(parents=True, exist_ok=True)
        for item in src.iterdir():
            if item.name in {".git", "__pycache__"}:
                continue
            dest = glass_dir / item.name
            if item.is_dir():
                shutil.copytree(item, dest, dirs_exist_ok=True)
            else:
                shutil.copy2(item, dest)
        os.chmod(glass_dir / "glass", 0o755)
        wrapper = bin_dir / "glass"
        if wrapper.exists() or wrapper.is_symlink():
            wrapper.unlink()
        wrapper.symlink_to(glass_dir / "glass")
        ui.success("glass CLI installed") if _has_glass() else ui.warn("glass installed but not on PATH (add ~/.local/bin).")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _manifest_path(path: str) -> Path:
    return Path(path) / MANIFEST_NAME


def _read_manifest(path: str) -> list[str]:
    manifest = _manifest_path(path)
    if not manifest.exists():
        return []
    if manifest.is_dir():
        ui.error(f"{MANIFEST_NAME} must be a manifest file, not a directory.")
        return []
    patterns = []
    for line in manifest.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            patterns.append(line)
    return patterns


def _resolve_manifest_path(root: Path, pattern: str) -> list[Path]:
    import glob

    raw = Path(os.path.expanduser(pattern))
    if not raw.is_absolute():
        raw = root / raw
    return [Path(p).resolve() for p in glob.glob(str(raw), recursive=True)]


def _build_manifest_archive(path: str) -> tuple[bytes, list[str]]:
    root = Path(path).resolve()
    resolved = []
    for pattern in _read_manifest(path):
        resolved.extend(_resolve_manifest_path(root, pattern))

    existing = sorted({p for p in resolved if p.exists()}, key=lambda p: str(p))
    dirs = [p for p in existing if p.is_dir()]
    top = [
        p
        for p in existing
        if not any(p != d and str(p).startswith(str(d) + os.sep) for d in dirs)
    ]

    archive = tempfile.NamedTemporaryFile(prefix="silicon-backup-", suffix=".tar.gz", delete=False)
    archive.close()
    included: list[str] = []
    try:
        with tarfile.open(archive.name, "w:gz") as tf:
            for item in top:
                arcname = item.relative_to(root).as_posix() if item.is_relative_to(root) else item.name
                try:
                    tf.add(item, arcname=arcname)
                    included.append(arcname)
                except Exception:
                    pass
        data = Path(archive.name).read_bytes()
    finally:
        Path(archive.name).unlink(missing_ok=True)
    return data, included


def _multipart(fields: dict[str, str], file_field: str, filename: str, content_type: str, data: bytes) -> tuple[bytes, str]:
    boundary = f"silicon-{uuid_hex()}"
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.extend([
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
            str(value).encode(),
            b"\r\n",
        ])
    chunks.extend([
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'.encode(),
        f"Content-Type: {content_type}\r\n\r\n".encode(),
        data,
        b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ])
    return b"".join(chunks), boundary


def uuid_hex() -> str:
    import uuid

    return uuid.uuid4().hex


def _get_json_with_silicon_key(url: str, api_key: str) -> tuple[int, dict]:
    req = urllib.request.Request(
        url,
        headers={
            "X-Silicon-Key": api_key,
            "Accept": "application/json",
            "User-Agent": "silicon-cli",
        },
    )
    try:
        with _urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception:
            return e.code, {}
    except Exception as e:
        return 0, {"detail": str(e)}


def _post_json_with_team_key(url: str, team_key: str, body=None) -> tuple[int, dict]:
    data = json.dumps(body or {}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "X-Team-Key": team_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "silicon-cli",
        },
    )
    try:
        with _urlopen(req, timeout=60) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception:
            return e.code, {}
    except Exception as e:
        return 0, {"detail": str(e)}


def _post_json(url: str, body=None, *, timeout: int = 60) -> tuple[int, dict]:
    data = json.dumps(body or {}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "silicon-cli",
        },
    )
    try:
        with _urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception:
            return e.code, {}
    except Exception as e:
        return 0, {"detail": str(e)}


def _team_slug_from_silicon(silicon: dict) -> str:
    return str(
        silicon.get("team")
        or silicon.get("owner_team_slug")
        or silicon.get("team_slug")
        or ""
    ).strip()


def _team_api_keys_url(server: str, team_slug: str) -> str:
    return f"{server}/api/v1/teams/{urllib.parse.quote(team_slug)}/api-keys"


def _fetch_team_api_keys(server: str, api_key: str, team_slug: str) -> tuple[int, dict]:
    return _get_json_with_silicon_key(_team_api_keys_url(server, team_slug), api_key)


def _team_key_rows(body: dict) -> list[dict]:
    rows = body.get("keys") if isinstance(body, dict) else None
    return rows if isinstance(rows, list) else []


def _key_row_by_name(rows: list[dict]) -> dict[str, dict]:
    out = {}
    for row in rows:
        if isinstance(row, dict):
            name = str(row.get("key_name") or "").strip().upper()
            if name:
                out[name] = row
    return out


def _configured_team_key_names(rows: list[dict]) -> list[str]:
    by_name = _key_row_by_name(rows)
    configured = []
    for key_name, _label in PROVIDER_API_KEYS:
        row = by_name.get(key_name, {})
        if (
            row.get("configured")
            or row.get("team_configured")
            or row.get("center_configured")
            or row.get("server_fallback_configured")
        ):
            configured.append(key_name)
    return configured


def _display_team_api_keys(rows: list[dict]) -> None:
    by_name = _key_row_by_name(rows)
    ui.info("Provider API token status from Glass (secrets are not returned):")
    for key_name, label in PROVIDER_API_KEYS:
        row = by_name.get(key_name, {})
        source = str(row.get("source") or "").strip()
        if row.get("team_configured") or source == "team":
            status = "team override"
        elif row.get("center_configured") or source == "center":
            status = "center managed"
        elif row.get("server_fallback_configured") or source == "server":
            status = "server fallback"
        elif row.get("configured"):
            status = "Glass managed"
        elif row.get("server_fallback_configured"):
            status = "server fallback only"
        else:
            status = "missing"
        ui.info(f"  {label} ({key_name}): {status}")


def _provider_key_env_from_rows(team_slug: str, rows: list[dict], prompt: str = "") -> dict[str, str]:
    if not rows:
        ui.warn("Glass did not return provider API token metadata.")
        return {}

    _display_team_api_keys(rows)
    configured = _configured_team_key_names(rows)
    missing = [key_name for key_name, _label in PROVIDER_API_KEYS if key_name not in configured]
    if missing:
        ui.warn("Some provider API tokens are not saved in Glass for this team.")
        ui.info("Set them in Glass > API keys, then rerun silicon pull if these silicons need them.")
        return {}

    return {
        "SILICON_PROVIDER_KEYS_SOURCE": "glass",
        "SILICON_PROVIDER_KEYS_TEAM": team_slug,
        "SILICON_PROVIDER_KEYS": ",".join(configured),
    }


def _choose_glass_provider_keys(server: str, api_key: str, silicon: dict) -> dict[str, str]:
    team_slug = _team_slug_from_silicon(silicon)
    if not team_slug:
        ui.warn("Glass did not return a team slug; provider API token status was not checked.")
        return {}

    code, body = _fetch_team_api_keys(server, api_key, team_slug)
    if not (200 <= code < 300):
        ui.warn(body.get("detail") or body.get("error") or f"Could not read provider API tokens from Glass (HTTP {code}).")
        return {}

    return _provider_key_env_from_rows(
        team_slug,
        _team_key_rows(body),
        "Use these Glass-managed provider API tokens for this silicon?",
    )


def _safe_instance_name(raw: str, fallback: str = "silicon") -> str:
    value = (raw or "").strip().lower()
    value = "".join(c if c.isalnum() or c in "._-" else "-" for c in value)
    value = "-".join(part for part in value.split("-") if part)
    return value.strip("._-") or fallback


def _choose_target(label: str, silicon_id: str) -> tuple[str, Path]:
    name = label
    if registry.name_taken(name):
        suffix = silicon_id[-6:].lower() if silicon_id else uuid_hex()[:6]
        name = f"{name}-{suffix}"

    if docker_runtime.enabled():
        base = Path(docker_runtime.load_config(required=True)["root"]).expanduser()
    else:
        base = Path.cwd()
    target = base / name
    if not target.exists():
        return name, target

    if not ui.interactive():
        ui.error(f"Target folder already exists: {target}")
        sys.exit(1)

    while target.exists():
        name = ui.ask("Target folder name", f"{label}-{uuid_hex()[:6]}")
        target = base / _safe_instance_name(name, "silicon")
    return target.name, target


def _write_dotenv(path: Path, values: dict[str, str]) -> None:
    lines = []
    existing = {}
    if path.exists():
        for raw in path.read_text().splitlines():
            if "=" in raw and not raw.lstrip().startswith("#"):
                key, value = raw.split("=", 1)
                existing[key.strip()] = value.strip()
            else:
                lines.append(raw)
    existing.update({k: v for k, v in values.items() if v is not None})
    rendered = [line for line in lines if line.strip()]
    rendered.extend(f"{key}={value}" for key, value in existing.items())
    path.write_text("\n".join(rendered).rstrip() + "\n")


def _seed_glass_files(
    target: Path,
    *,
    server: str,
    api_key: str,
    silicon: dict,
    instance_name: str,
    provider_key_env: dict[str, str] | None = None,
) -> None:
    silicon_id = str(silicon.get("silicon_id") or "").strip()
    silicon_name = str(silicon.get("name") or instance_name).strip()
    glass = {
        "server_url": server,
        "silicon_id": silicon_id,
        "silicon_username": silicon_name,
        "name": silicon_name,
        "api_key": api_key,
        "silicon_api_key": api_key,
    }
    target.mkdir(parents=True, exist_ok=True)
    (target / ".glass.json").write_text(json.dumps(glass, indent=2) + "\n")
    env_values = {
        "GLASS_SERVER_URL": server,
        "GLASS_API_KEY": api_key,
        "SILICON_UPDATE_AUTH_KEY": api_key,
        "SILICON_ID": silicon_id,
        "SILICON_NAME": silicon_name,
    }
    if provider_key_env:
        env_values.update(provider_key_env)
    _write_dotenv(target / ".env", env_values)
    stemcell._env_upsert(target / "env.py", "GLASS_API_KEY", api_key)

    config = {}
    sj = target / "silicon.json"
    if sj.exists():
        try:
            config = json.loads(sj.read_text())
        except json.JSONDecodeError:
            config = {}
    config.update(
        {
            "name": silicon_name,
            "address": instance_name,
            "silicon_id": silicon_id,
            "glass": glass,
        }
    )
    config.setdefault("run", "python main.py")
    config.setdefault("brain", "claude")
    config.setdefault(
        "workers",
        {"browser": ["claude"], "terminal": ["claude"], "writer": ["claude"]},
    )
    config.setdefault("brain_order", [config.get("brain", "claude")])
    sj.write_text(json.dumps(config, indent=4) + "\n")


def _glass_config(path: str) -> dict:
    cfg = Path(path) / ".glass.json"
    if not cfg.exists():
        raise FileNotFoundError("No .glass.json found.")
    return json.loads(cfg.read_text())


def _manifest_backup_now(path: str, note: str = "manual") -> bool:
    patterns = _read_manifest(path)
    if not patterns:
        ui.warn("No .backupsilicon manifest found. Nothing to back up.")
        return False

    data, included = _build_manifest_archive(path)
    if not included:
        ui.warn(".backupsilicon matched no files.")
        return False

    cfg = _glass_config(path)
    api_key = cfg.get("api_key") or cfg.get("silicon_api_key") or ""
    if not api_key:
        ui.error(".glass.json does not contain api_key.")
        return False

    server = (cfg.get("server_url") or GLASS_SERVER_URL).rstrip("/")
    body, boundary = _multipart(
        {"manifest": json.dumps(included), "note": note},
        "file",
        "backup.tar.gz",
        "application/gzip",
        data,
    )
    req = urllib.request.Request(
        server + BACKUP_UPLOAD_PATH,
        data=body,
        headers={
            "X-Silicon-Key": api_key,
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )
    try:
        with _urlopen(req, timeout=180) as resp:
            payload = json.loads(resp.read().decode() or "{}")
            ui.success(f"Backup uploaded v{payload.get('seq', '?')} ({len(included)} paths).")
            return True
    except urllib.error.HTTPError as e:
        msg = e.read().decode(errors="replace")[:200]
        ui.error(f"Backup upload failed HTTP {e.code}: {msg}")
    except Exception as e:
        ui.error(f"Backup upload failed: {e}")
    return False


def backup_loop(path: str, name: str | None = None) -> None:
    label = name or Path(path).name
    while True:
        wait = _seconds_until_next_backup()
        ui.info(f"Next scheduled backup for '{label}' at 23:59 GMT.")
        time.sleep(wait)
        ui.info(f"Running scheduled backup for '{label}'...")
        _manifest_backup_now(path, note="scheduled")


def _seconds_until_next_backup(now: datetime | None = None) -> float:
    now = now or datetime.now(timezone.utc)
    target = now.replace(
        hour=BACKUP_HOUR_UTC,
        minute=BACKUP_MINUTE_UTC,
        second=0,
        microsecond=0,
    )
    if target <= now:
        target += timedelta(days=1)
    return max(1.0, (target - now).total_seconds())


def _team_pull_url(server: str) -> str:
    return f"{server}/api/v1/teams/setup-pull"


def _browser_profile_setup_start_url(server: str) -> str:
    return f"{server}/api/v1/browser-profiles/setup/start"


def _browser_profile_setup_finish_url(server: str) -> str:
    return f"{server}/api/v1/browser-profiles/setup/finish"


def _browser_profile_finish_command(token: str, session_id: str, before_ids: list[str]) -> str:
    before = ",".join(before_ids)
    return f"silicon browser-profile finish '{token}' '{session_id}' '{before}'"


def browser_profile_finish(token: str | None, session_id: str | None, before_ids_csv: str | None = None) -> None:
    token = (token or "").strip()
    session_id = (session_id or "").strip()
    if not token or not session_id:
        ui.error("Usage: silicon browser-profile finish <setup_token> <session_id> [before_profile_ids_csv]")
        sys.exit(1)
    before_ids = [p.strip() for p in (before_ids_csv or "").split(",") if p.strip()]
    server = GLASS_SERVER_URL.rstrip("/")
    ui.info("Finishing browser profile setup with Glass...")
    code, body = _post_json(
        _browser_profile_setup_finish_url(server),
        {"token": token, "session_id": session_id, "before_profile_ids": before_ids},
        timeout=120,
    )
    if not (200 <= code < 300):
        ui.error(body.get("detail") or body.get("error") or f"Glass could not finish the profile setup (HTTP {code}).")
        sys.exit(1)
    profile = body.get("profile") or {}
    name = profile.get("name") or profile.get("id") or "browser profile"
    assigned = body.get("assigned", 0)
    ui.success(f"Saved Steel profile '{name}' and assigned it to {assigned} silicon(s).")


def browser_profile_setup(token: str | None) -> None:
    token = (token or "").strip()
    if not token:
        ui.error("Usage: silicon browser-profile setup <setup_token>")
        sys.exit(1)
    server = GLASS_SERVER_URL.rstrip("/")
    ui.info("Starting browser profile setup with Glass...")
    code, body = _post_json(_browser_profile_setup_start_url(server), {"token": token}, timeout=120)
    if not (200 <= code < 300):
        ui.error(body.get("detail") or body.get("error") or f"Glass could not start the profile setup (HTTP {code}).")
        sys.exit(1)

    session_id = str(body.get("session_id") or "").strip()
    viewer_url = str(body.get("viewer_url") or body.get("debug_url") or "").strip()
    before_ids = [str(p) for p in (body.get("before_profile_ids") or []) if p]
    if not session_id or not viewer_url:
        ui.error("Glass returned an incomplete browser setup session.")
        sys.exit(1)

    ui.success("Steel setup session started.")
    ui.info(f"Viewer URL: {viewer_url}")
    try:
        webbrowser.open(viewer_url)
    except Exception:
        pass

    finish_cmd = _browser_profile_finish_command(token, session_id, before_ids)
    if not ui.interactive():
        ui.info("When the browser is configured, finish with:")
        print(f"  {finish_cmd}")
        return

    print()
    ui.info("Use the browser window to log in or configure the profile.")
    ui.info("Press Enter here when you're done. Ctrl+C leaves the session open; finish later with:")
    print(f"  {finish_cmd}")
    try:
        input()
    except KeyboardInterrupt:
        print()
        ui.warn("Setup session left open.")
        ui.info(f"Finish later with: {finish_cmd}")
        return

    browser_profile_finish(token, session_id, ",".join(before_ids))


def _silicon_display(silicon: dict) -> str:
    name = str(silicon.get("name") or "").strip()
    sid = str(silicon.get("silicon_id") or "").strip()
    return f"{name} ({sid})" if name and sid else name or sid or "silicon"


def _select_silicons_for_custom_settings(silicons: list[dict]) -> set[str]:
    if not ui.interactive() or len(silicons) <= 1:
        return set()
    if ui.confirm("Use these same settings for all silicons?", default_yes=True):
        return set()

    ui.info("Select silicons that need different settings. You can use numbers, names, or silicon IDs.")
    lookup: dict[str, str] = {}
    for idx, silicon in enumerate(silicons, start=1):
        sid = str(silicon.get("silicon_id") or "").strip()
        name = str(silicon.get("name") or "").strip()
        ui.info(f"  {idx}. {_silicon_display(silicon)}")
        if sid:
            lookup[str(idx)] = sid
            lookup[sid.lower()] = sid
        if name:
            lookup[name.lower()] = sid

    raw = ui.ask("Different settings for", "")
    selected: set[str] = set()
    for part in raw.replace(";", ",").split(","):
        key = part.strip().lower()
        if key and key in lookup:
            selected.add(lookup[key])
    if raw.strip() and not selected:
        ui.warn("No matching silicons selected; using the default settings for all.")
    return selected


def _team_setup_configs(silicons: list[dict], opts: PullOpts | None = None) -> dict[str, dict]:
    kw = opts.setup_config_kwargs() if opts else {}
    base = stemcell.choose_setup_config("Default setup for all team silicons", **kw)
    # Glass may specify a brain per silicon (from the Create Team wizard). When it
    # does, honor those and skip the interactive per-silicon prompt.
    glass_has_brains = any((s.get("brain") or "").strip() for s in silicons)
    custom = set()
    if not glass_has_brains and not (opts and (opts.assume_yes or opts.brain)):
        custom = _select_silicons_for_custom_settings(silicons)
    configs: dict[str, dict] = {}
    for silicon in silicons:
        sid = str(silicon.get("silicon_id") or "").strip()
        b = (silicon.get("brain") or "").strip().lower()
        if b in ("claude", "codex"):
            order = [x for x in (silicon.get("brain_order") or [b]) if x in ("claude", "codex")] or [b]
            configs[sid] = stemcell.choose_setup_config("", brain=b, brain_order=order)
        else:
            configs[sid] = base
    for silicon in silicons:
        sid = str(silicon.get("silicon_id") or "").strip()
        if sid in custom:
            configs[sid] = stemcell.choose_setup_config(f"Setup for {_silicon_display(silicon)}")
    return configs


def _provider_keys_from_team_pull(body: dict) -> dict[str, str]:
    team = body.get("team") if isinstance(body, dict) else {}
    team_slug = str((team or {}).get("slug") or "").strip()
    api_keys = body.get("api_keys") if isinstance(body, dict) else {}
    if not team_slug:
        ui.warn("Glass did not return a team slug; provider API token status was not checked.")
        return {}
    return _provider_key_env_from_rows(
        team_slug,
        _team_key_rows(api_keys if isinstance(api_keys, dict) else {}),
        "Use these Glass-managed provider API tokens for all pulled silicons?",
    )


def _pull_team(api_key: str, server: str, opts: PullOpts | None = None) -> None:
    ui.info("Checking team setup token with Glass...")
    code, body = _post_json_with_team_key(_team_pull_url(server), api_key, {})
    if not (200 <= code < 300):
        ui.error(body.get("detail") or body.get("error") or f"Glass rejected the team token (HTTP {code}).")
        sys.exit(1)

    team = body.get("team") if isinstance(body, dict) else {}
    team_name = str((team or {}).get("name") or (team or {}).get("slug") or "team").strip()
    silicons = body.get("silicons") if isinstance(body, dict) else []
    silicons = [s for s in silicons if isinstance(s, dict) and str(s.get("silicon_id") or "").strip()]
    if not silicons:
        ui.error(f"Glass returned no silicons for team '{team_name}'.")
        sys.exit(1)

    provider_key_env = _provider_keys_from_team_pull(body)
    setup_configs = _team_setup_configs(silicons, opts)
    pulled: list[tuple[str, Path]] = []

    for silicon in silicons:
        silicon_id = str(silicon.get("silicon_id") or "").strip()
        silicon_name = str(silicon.get("name") or "").strip()
        silicon_key = str(silicon.get("api_key") or "").strip()
        if not silicon_key:
            ui.warn(f"Skipping {_silicon_display(silicon)}; Glass did not return a silicon API key.")
            continue

        default_name = _safe_instance_name(silicon_name, f"silicon-{silicon_id[-6:].lower()}")
        instance_name, target = _choose_target(default_name, silicon_id)
        try:
            _seed_glass_files(
                target,
                server=server,
                api_key=silicon_key,
                silicon=silicon,
                instance_name=instance_name,
                provider_key_env=provider_key_env,
            )
            stemcell.hydrate(
                str(target),
                setup_config=setup_configs.get(silicon_id),
                install_deps=not docker_runtime.enabled(),
                setup_interface=not docker_runtime.enabled(),
                register_install=not docker_runtime.enabled(),
            )
            if docker_runtime.enabled():
                docker_runtime.register_instance(instance_name, target)
            pulled.append((instance_name, target))
        except Exception:
            if target.exists() and not any(target.iterdir()):
                shutil.rmtree(target, ignore_errors=True)
            raise

    if not pulled:
        ui.error("No silicons were pulled.")
        sys.exit(1)

    ui.success(f"Pulled {len(pulled)} silicon(s) from team '{team_name}'.")
    for instance_name, target in pulled:
        ui.info(f"  {instance_name}: {target}")

    ui.info("Starting pulled silicons...")
    for instance_name, _target in pulled:
        process.start_one(instance_name)

    if _want_backups(opts):
        for instance_name, target in pulled:
            ui.info(f"Running initial backup for '{instance_name}'...")
            ok = _manifest_backup_now(str(target), note="initial")
            if ok:
                _start_backup_loop(str(target), instance_name)
            else:
                ui.warn(f"Initial backup failed. Retry with: silicon backup {instance_name} now")


def _want_backups(opts: PullOpts | None) -> bool:
    """Whether to enable daily backups. An explicit ``--backup/--no-backup`` wins;
    otherwise ask (or take the non-TTY default). ``--yes`` implies yes."""
    if opts and opts.backup is not None:
        return opts.backup
    if opts and opts.assume_yes:
        return True
    if not ui.interactive():
        return False
    return ui.confirm("Enable daily 23:59 UTC backups for all pulled silicons?")


def pull(api_token: str | None, opts: PullOpts | None = None) -> None:
    api_key = (api_token or "").strip()
    if not api_key:
        ui.info("Paste the team setup token generated from Glass.")
        api_key = ui.read_secret("Glass team setup token").strip()
    if not api_key:
        ui.error("Usage: silicon pull <api_token>")
        sys.exit(1)

    docker_runtime.ensure_pull_runtime()

    server = GLASS_SERVER_URL.rstrip("/")
    if api_key.startswith("sct_live_"):
        _pull_team(api_key, server, opts)
        return

    ui.info("Checking token with Glass...")
    code, silicon = _get_json_with_silicon_key(f"{server}/api/v1/silicons/me", api_key)
    if not (200 <= code < 300):
        ui.error(silicon.get("detail") or silicon.get("error") or f"Glass rejected the token (HTTP {code}).")
        sys.exit(1)

    silicon_id = str(silicon.get("silicon_id") or "").strip()
    silicon_name = str(silicon.get("name") or "").strip()
    if not silicon_id:
        ui.error("Glass did not return a silicon_id for this token.")
        sys.exit(1)

    default_name = _safe_instance_name(
        (opts.name if opts and opts.name else silicon_name),
        f"silicon-{silicon_id[-6:].lower()}",
    )
    instance_name, target = _choose_target(default_name, silicon_id)
    provider_key_env = _choose_glass_provider_keys(server, api_key, silicon)
    setup_config = stemcell.choose_setup_config("", **opts.setup_config_kwargs()) if (
        opts and opts.brain
    ) else None

    try:
        _seed_glass_files(
            target,
            server=server,
            api_key=api_key,
            silicon=silicon,
            instance_name=instance_name,
            provider_key_env=provider_key_env,
        )
        stemcell.hydrate(
            str(target),
            setup_config=setup_config,
            install_deps=not docker_runtime.enabled(),
            setup_interface=not docker_runtime.enabled(),
            register_install=not docker_runtime.enabled(),
        )
        if docker_runtime.enabled():
            docker_runtime.register_instance(instance_name, target)
    except Exception:
        if target.exists() and not any(target.iterdir()):
            shutil.rmtree(target, ignore_errors=True)
        raise

    ui.success(f"Pulled Glass silicon '{silicon_name or silicon_id}' into {target}")
    ui.info(f"Registered as '{instance_name}'.")
    process.start_one(instance_name)

    if _want_backups(opts):
        ui.info("Running initial backup...")
        ok = _manifest_backup_now(str(target), note="initial")
        if ok:
            _start_backup_loop(str(target), instance_name)
        else:
            ui.warn(f"Initial backup failed. Retry with: silicon backup {instance_name} now")


def _start_backup_loop(path: str, name: str) -> None:
    ui.info("Starting daily 23:59 GMT backup loop in background...")
    log = open(Path(path) / ".glass-push.log", "a")
    if _manifest_path(path).exists():
        cmd = [sys.executable, "-m", "silicon_cli.cli", "_backup_loop", path, name]
        proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    else:
        proc = subprocess.Popen(["glass", "push"], cwd=path, stdout=log, stderr=subprocess.STDOUT,
                                start_new_session=True)
    (Path(path) / ".glass-push.pid").write_text(str(proc.pid))
    ui.success(f"Daily backups running (PID {proc.pid}). Logs: {path}/.glass-push.log")
    ui.info(f"Use 'silicon push {name} now' for a manual backup anytime.")


def push(target: str | None, subcmd: str | None) -> None:
    inst = registry.resolve_one(target)
    if not (Path(inst.path) / ".glass.json").exists():
        ui.error(f"'{inst.name}' is not connected to Glass. No .glass.json found.")
        sys.exit(1)
    if inst.is_docker:
        args = ["push", inst.name]
        if subcmd:
            args.append(subcmd)
        if subcmd != "now" and not docker_runtime.container_running(inst):
            if subcmd == "stop":
                ui.warn(f"'{inst.name}' container is not running.")
                return
            docker_runtime.start_one(inst)
        code = docker_runtime.run_silicon(inst, args)
        if code:
            sys.exit(code)
        return
    ensure_glass_cli()
    pid_file = Path(inst.path) / ".glass-push.pid"

    if subcmd == "now":
        ui.info(f"Pushing '{inst.name}' to Glass...")
        if _manifest_path(inst.path).exists():
            ok = _manifest_backup_now(inst.path, note="manual")
        else:
            ok = subprocess.run(["glass", "push", "now"], cwd=inst.path).returncode == 0
        ui.success("Backup complete.") if ok else ui.error("Push failed.")
    elif subcmd == "stop":
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 15)
            pid_file.unlink(missing_ok=True)
            ui.success(f"Stopped backup loop for '{inst.name}'.")
            return
        except Exception:
            ui.warn(f"No backup loop running for '{inst.name}'.")
            pid_file.unlink(missing_ok=True)
    else:
        if pid_file.exists():
            try:
                pid = int(pid_file.read_text().strip())
                os.kill(pid, 0)
                ui.warn(f"Backup loop already running for '{inst.name}' (PID {pid})")
                return
            except Exception:
                pass
        ui.info(f"Starting daily 23:59 GMT backup loop for '{inst.name}'...")
        subprocess.run(["glass", "push", "now"], cwd=inst.path)
        _start_backup_loop(inst.path, inst.name)
