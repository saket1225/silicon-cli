"""silicon — manage your silicon instances. Dispatch mirrors the original bash CLI."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from . import (
    docker_runtime,
    glassagent,
    package_manager,
    process,
    registry,
    stemcell,
    sync,
    ui,
    update,
)
from .config import active_release_root, python_run_cmd, runtime_environment

COMMANDS = ["start", "stop", "restart", "status", "browser", "debug", "attach",
            "pull", "push", "backup", "update", "update-check", "check-update",
            "browser-profile", "list", "install", "new", "help", "script", "agent",
            "docker", "claude", "codex", "package"]


def _parse_pull_args(args: list[str]) -> tuple[str | None, sync.PullOpts]:
    """Parse ``silicon pull [token] [flags]`` into (token, PullOpts).

    Flags let Glass's setup agent drive the pull with no prompts:
      -y/--yes            take every prompt's default
      --brain B           claude | codex | both
      --brain-order CSV   e.g. claude,codex
      --backup/--no-backup enable/disable daily backups
      --runtime R         docker | local (sets SILICON_RUNTIME)
      --name NAME         instance name (single-silicon pull)
    """
    opts = sync.PullOpts()
    token: str | None = None
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-y", "--yes"):
            opts.assume_yes = True
        elif a == "--brain" and i + 1 < len(args):
            opts.brain = args[i + 1].strip().lower()
            i += 1
        elif a == "--brain-order" and i + 1 < len(args):
            opts.brain_order = [
                b.strip().lower()
                for b in args[i + 1].split(",")
                if b.strip()
            ]
            i += 1
        elif a == "--backup":
            opts.backup = True
        elif a == "--no-backup":
            opts.backup = False
        elif a == "--name" and i + 1 < len(args):
            opts.name = args[i + 1].strip()
            i += 1
        elif a == "--runtime" and i + 1 < len(args):
            rt = args[i + 1].strip().lower()
            import os
            os.environ["SILICON_RUNTIME"] = rt
            i += 1
        elif not a.startswith("-") and token is None:
            token = a
        i += 1
    return token, opts


# ----------------------------------------------------------------- commands
def cmd_list() -> None:
    rows = registry.installs()
    if not rows:
        ui.info("No silicon installations found.")
        ui.info("Run 'silicon install' to set up a new instance.")
        return
    print(f"\n{ui.BOLD}{ui.CYAN}Silicon Installations{ui.RESET}\n")
    print(f"  {ui.DIM}{'#':<4}{'NAME':<22}{'STATUS':<10}PATH{ui.RESET}")
    print(f"  {ui.DIM}{'---':<4}{'----':<22}{'------':<10}----{ui.RESET}")
    for i in rows:
        if process.install_is_running(i):
            pid = process.get_pid(i.pid_file)
            status = f"{ui.GREEN}● running{ui.RESET}"
            extra = f" {ui.DIM}(Docker){ui.RESET}" if i.is_docker else f" {ui.DIM}(PID {pid}){ui.RESET}"
        else:
            status = f"{ui.DIM}○ stopped{ui.RESET}"
            extra = f" {ui.DIM}(Docker){ui.RESET}" if i.is_docker else ""
        print(f"  {i.index + 1:<4}{i.name:<22}{status}{extra}  {ui.DIM}{i.path}{ui.RESET}")
    print()


def _print_status(inst) -> None:
    if inst.is_docker:
        docker_runtime.print_status(inst)
        return
    if process.is_running(inst.pid_file):
        pid = process.get_pid(inst.pid_file)
        print(f"\n{ui.BOLD}{inst.name}{ui.RESET} {ui.GREEN}● running{ui.RESET} (PID {pid})")
    else:
        print(f"\n{ui.BOLD}{inst.name}{ui.RESET} {ui.DIM}○ stopped{ui.RESET}")
    print(f"{ui.DIM}  Path: {inst.path}{ui.RESET}\n")


def cmd_status(target: str | None) -> None:
    if target:
        inst = registry.find(target)
        if not inst:
            cmd_list()
            return
        _print_status(inst)
        return
    inst = registry.find()
    if inst:
        _print_status(inst)
    else:
        cmd_list()


def cmd_browser(target: str | None) -> None:
    inst = registry.resolve_one(target)
    if inst.is_docker:
        docker_runtime.run_silicon(inst, ["browser", inst.name])
        return
    ui.info(f"Opening browser for '{inst.name}'...")
    subprocess.run(
        [
            python_run_cmd(inst.path),
            str(active_release_root(inst.path) / "main.py"),
            "browser",
        ],
        cwd=inst.path,
        env=runtime_environment(inst.path),
    )


def cmd_debug(target: str | None) -> None:
    inst = registry.resolve_one(target)
    if inst.is_docker:
        docker_runtime.debug(inst)
        return
    if not process.install_is_running(inst):
        ui.error(f"'{inst.name}' is not running. Start it first with: silicon start {inst.name}")
        sys.exit(1)
    log_file = Path(inst.path) / ".silicon.log"
    if not log_file.exists():
        ui.error(f"No log file found at {log_file}")
        sys.exit(1)
    pid = process.get_pid(inst.pid_file)
    print(f"\n{ui.BOLD}{ui.CYAN}Debugging '{inst.name}'{ui.RESET} (PID {pid})")
    print(f"{ui.DIM}  Log: {log_file}{ui.RESET}")
    print(f"{ui.DIM}  Press Ctrl+C to detach{ui.RESET}\n")
    try:
        subprocess.run(["tail", "-f", str(log_file)])
    except KeyboardInterrupt:
        pass


def cmd_attach(target_dir: str | None) -> None:
    target = Path(target_dir or ".").resolve()
    if not (target / "main.py").exists() or not (target / "config.py").exists():
        ui.error("This doesn't look like a silicon directory.")
        ui.info(f"Expected main.py and config.py in: {target}")
        ui.info("  silicon attach /path/to/silicon")
        sys.exit(1)
    if not (target / "prompts").is_dir() or not (target / "core").is_dir():
        ui.error("Missing prompts/ or core/ directory. Not a valid silicon.")
        sys.exit(1)

    for i in registry.installs():
        if i.path == str(target):
            ui.warn(f"This silicon is already registered as '{i.name}'")
            return

    ui.success(f"Found a silicon at: {target}")
    name = ui.ask("Instance name", target.name)
    if registry.name_taken(name):
        ui.error(f"Name '{name}' is already taken. Pick a different one.")
        sys.exit(1)

    pid_file = target / ".silicon.pid"
    running = process.is_running(str(pid_file))
    registry.register(name, str(target), str(pid_file))
    ui.success(f"Attached '{name}' at {target}")
    if running:
        print(f"\n  {ui.BOLD}{name}{ui.RESET} {ui.GREEN}● running{ui.RESET} (PID {process.get_pid(str(pid_file))})\n")
    else:
        print(f"\n  {ui.BOLD}{name}{ui.RESET} {ui.DIM}○ stopped{ui.RESET}")
        print(f"  Start it with: {ui.BOLD}silicon start {name}{ui.RESET}\n")


def cmd_agent(subcmd: str | None, target: str | None) -> None:
    if not subcmd:
        ui.error("Usage: silicon agent <start|stop|status> [name]")
        sys.exit(1)
    inst = registry.resolve_one(target)
    if inst.is_docker:
        code = docker_runtime.run_silicon(inst, ["agent", subcmd, inst.name])
        if code:
            sys.exit(code)
        return
    if subcmd == "start":
        glassagent.start(inst.path)
    elif subcmd == "stop":
        glassagent.stop(inst.path)
    elif subcmd == "status":
        if glassagent.status(inst.path):
            agent_pid = glassagent.pid(inst.path)
            print(
                f"{ui.GREEN}●{ui.RESET} Glass agent running "
                f"(PID {agent_pid})"
            )
        else:
            print(f"{ui.DIM}○{ui.RESET} Glass agent stopped")
    else:
        ui.error(f"Unknown agent command: {subcmd}. Use start, stop, or status.")
        sys.exit(1)


def cmd_new(target: str | None) -> None:
    if docker_runtime.runtime_requested():
        docker_runtime.ensure_ready(
            auto_init=True,
            install=True,
            pull_image=False,
            quiet=True,
            write_compose=False,
        )
        docker_target = docker_runtime.target_path(target)
        stemcell.hydrate(
            str(docker_target),
            install_deps=False,
            setup_interface=False,
            register_install=False,
            bind_docker_runtime=True,
        )
        name = docker_target.name
        sj = docker_target / "silicon.json"
        if sj.exists():
            import json

            try:
                data = json.loads(sj.read_text())
                name = (data.get("address") or data.get("name") or name).strip() or name
            except Exception:
                pass
        docker_runtime.register_instance(name, docker_target)
        ui.info(f"Run 'silicon start {name}' when you're ready.")
        return
    if target:
        stemcell.hydrate(target)
        return
    # No target: ask for a folder to create (Python-native installer).
    name = ui.ask("New silicon folder name", "silicon")
    if not name:
        ui.error("A folder name is required.")
        sys.exit(1)
    stemcell.hydrate(str(Path.cwd() / name))


def cmd_help() -> None:
    print(f"""
{ui.BOLD}{ui.CYAN}silicon{ui.RESET} – manage your silicon instances

{ui.BOLD}Usage:{ui.RESET}
  silicon                     Show status or list instances
  silicon new [dir]           Create a new Silicon (hydrate from stemcell)
  silicon new .               Hydrate the current folder into a runnable silicon
  silicon start <target>      Start silicon(s). target = name, *, all, 1,2,4, or name,name
  silicon stop <target>       Stop silicon(s) (agent stays alive)
  silicon stop --full <target> Stop silicon(s) and glass agent
  silicon restart <target>    Restart silicon(s)
  silicon agent <start|stop|status> [name]  Manage glass agent
  silicon status [name]       Show instance status
  silicon browser [name]      Open headed browser for login
  silicon browser-profile setup
                             Create a cloud browser profile through Glass;
                             paste the token into the hidden prompt
  silicon browser-profile finish <session_id>
                             Finish a browser profile setup session
  silicon debug [name]        Attach to running instance (live logs)
  silicon attach [path]       Register an existing silicon instance
  silicon pull [api_token]    Pull a Glass team or silicon into local folders
                              flags: -y/--yes --brain <claude|codex|both>
                              --brain-order a,b --backup/--no-backup
                              --runtime <docker|local> --name <name>
  silicon push [name]         Start daily 23:59 GMT backup loop to Glass
  silicon push [name] now     Push a one-time backup to Glass
  silicon push [name] stop    Stop the daily backup loop
  silicon push [name] status  Show supervisor health and next attempt
  silicon backup [name] [now|stop|status] Alias for silicon push
  silicon update <target>     Stage, drain, checkpoint, update, validate, and restart
  silicon update --dry-run <target>  Show the exact merge/update plan
  silicon update all --concurrency 8 --canary-count 1
                              Canary-first bounded-parallel fleet rollout
  silicon update all --all-at-once  Maximum speed with no canary barrier
  silicon update prewarm [target]   Prepare local Python generations without activation
  silicon update status [name]       Show generation and transaction state
  silicon update cancel [name]       Cancel an update before its stop boundary
  silicon update resume [name]       Resume an interrupted transaction
  silicon update history [name]      Show durable update history
  silicon update rollback [name]     Task-safely reactivate the prior generation
  silicon update check [name] Trigger this silicon's system update check now
  silicon update-check [name] Trigger this silicon's system update check now
  silicon list                List all instances
  silicon docker init         Legacy opt-in: enable Docker-backed installs
  silicon docker doctor       Legacy opt-in: check Docker runtime setup
  silicon docker login        Legacy opt-in: set up shared container auth
  silicon docker migrate-local [name|all]
                              Move fully stopped Docker Silicons to host-local
  silicon claude [args...]    Run Claude Code with shared Docker auth
  silicon codex [args...]     Run Codex with shared Docker auth
  silicon script update       Update the silicon CLI itself
  silicon package inventory   List host, Docker, and per-instance package versions
  silicon package update <package>
                              Safely update every selected copy of a package
  silicon install             Install a new instance
  silicon help                Show this help
""")


def suggest_command(inp: str) -> None:
    def score(cmd: str) -> int:
        ld = abs(len(inp) - len(cmd))
        if cmd.startswith(inp) or inp.startswith(cmd):
            return ld
        common = sum(1 for a, b in zip(inp, cmd) if a == b)
        return max(len(cmd), len(inp)) - common + ld
    best = min(COMMANDS + ["ls"], key=score)
    if score(best) <= 3:
        print(f"\n{ui.YELLOW}Did you mean?{ui.RESET}\n  silicon {best}\n")


# ----------------------------------------------------------------- dispatch
def main(argv: list[str] | None = None) -> None:
    argv = argv if argv is not None else sys.argv[1:]
    cmd = argv[0] if argv else ""
    a1 = argv[1] if len(argv) > 1 else None
    a2 = argv[2] if len(argv) > 2 else None

    if cmd == "_watchdog":  # internal: the supervised auto-restart loop
        process.watchdog_loop(name=a2 or "silicon", path=a1, pid_file=argv[3] if len(argv) > 3 else "")
        return

    if cmd == "_backup_loop":  # internal: the scheduled manifest backup loop
        sync.backup_loop(a1 or ".", a2, argv[3] if len(argv) > 3 else None)
        return

    if cmd == "_maintenance_reconcile":
        from .updater.maintenance import reconcile_pending_terminal

        reconcile_pending_terminal(Path(a1 or "."))
        return

    # A durable schedule intent survives host restarts even though its detached
    # process does not. Any normal CLI startup repairs missing supervisors.
    if cmd not in {"push", "backup"}:
        sync.reconcile_backup_schedules(quiet=True)

    if cmd == "start":
        process.start(a1)
    elif cmd == "stop":
        if a1 == "--full":
            process.stop(a2, full=True)
        else:
            process.stop(a1, full=(a2 == "--full"))
    elif cmd == "restart":
        process.restart(a1)
    elif cmd == "status":
        cmd_status(a1)
    elif cmd == "browser":
        cmd_browser(a1)
    elif cmd == "browser-profile":
        if a1 == "setup":
            sync.browser_profile_setup(a2)
        elif a1 == "finish":
            sync.browser_profile_finish(
                a2,
                argv[3] if len(argv) > 3 else None,
                argv[4] if len(argv) > 4 else None,
            )
        else:
            ui.error("Usage: silicon browser-profile <setup|finish> ...")
            sys.exit(1)
    elif cmd == "debug":
        cmd_debug(a1)
    elif cmd == "docker":
        docker_runtime.cmd_docker(argv[1:])
    elif cmd in ("claude", "codex"):
        docker_runtime.run_shared_tool(cmd, argv[1:])
    elif cmd == "attach":
        cmd_attach(a1)
    elif cmd == "pull":
        token, opts = _parse_pull_args(argv[1:])
        sync.pull(token, opts)
    elif cmd in ("push", "backup"):
        sync.push(a1, a2)
    elif cmd == "update":
        update.update_command(argv[1:])
    elif cmd in ("update-check", "check-update"):
        update.trigger_update_check(a1)
    elif cmd in ("list", "ls"):
        cmd_list()
    elif cmd == "agent":
        cmd_agent(a1, a2)
    elif cmd == "script":
        if a1 == "update":
            update.update_cli()
        else:
            ui.error(f"Unknown script command: {a1}. Did you mean: silicon script update?")
            sys.exit(1)
    elif cmd == "package":
        try:
            package_manager.package_command(argv[1:])
        except RuntimeError as exc:
            ui.error(str(exc))
            sys.exit(2)
    elif cmd == "new":
        cmd_new(a1)
    elif cmd == "install":
        cmd_new(None)
    elif cmd in ("help", "-h", "--help"):
        cmd_help()
    elif cmd == "":
        cmd_status(None)
    else:
        ui.error(f"Unknown command: {cmd}")
        suggest_command(cmd)
        sys.exit(1)


if __name__ == "__main__":
    main()
