"""``buildloop`` command line.

One entry point, everything else runs when asked:

    buildloop refresh            # all configured projects
    buildloop refresh steady     # one project
    buildloop install            # write config skeleton + link the init script
    buildloop status             # what's stored, without touching the network
"""

from __future__ import annotations

import argparse
import shutil
import sys
import webbrowser
from pathlib import Path

from . import __version__, ci_collector, config as config_mod, dashboard, db, gh, gradle_ingest

INIT_SCRIPT_NAME = "buildloop.init.gradle.kts"


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


# --- commands ---------------------------------------------------------------

def cmd_refresh(args) -> int:
    cfg = config_mod.load()
    targets = [cfg.project(n) for n in args.projects] if args.projects else list(cfg.projects)
    if not targets:
        print("no projects configured", file=sys.stderr)
        return 1

    # Rewritten every run so the Gradle init script's project gate can never
    # drift from the config.
    config_mod.write_gradle_projects(cfg)

    outputs: list[Path] = []
    with db.open_db(cfg.db_path) as conn:
        known = {p.name for p in cfg.projects}
        stats = gradle_ingest.ingest(conn, cfg.jsonl_path, known)
        print(f"local builds: {stats}")

        for project in targets:
            print(f"\n{project.name}:")
            if project.tracks_ci:
                try:
                    result = ci_collector.collect(conn, project, log=print)
                    print(f"  ci: {result}")
                except gh.GhError as exc:
                    print(f"  ci: FAILED — {exc}", file=sys.stderr)
            else:
                print("  ci: not configured (no github_repo)")

            out = cfg.home / f"dashboard-{project.name}.html"
            out.write_text(dashboard.render(conn, project.name), encoding="utf-8")
            outputs.append(out)
            print(f"  dashboard: {out}")

    if args.open and outputs:
        webbrowser.open(outputs[0].as_uri())
    return 0


def cmd_status(args) -> int:
    cfg = config_mod.load()
    print(f"home:   {cfg.home}")
    print(f"db:     {cfg.db_path} ({'exists' if cfg.db_path.exists() else 'not created yet'})")
    print(f"jsonl:  {cfg.jsonl_path} ({_size(cfg.jsonl_path)})")
    installed = _init_script_target()
    print(f"gradle: {installed} ({'installed' if installed.exists() else 'NOT installed'})")

    if not cfg.db_path.exists():
        print("\nnothing collected yet — run: buildloop refresh")
        return 0

    with db.open_db(cfg.db_path) as conn:
        print(f"\n{'project':<16}{'ci runs':>9}{'ci jobs':>9}{'builds':>8}  latest run")
        for p in cfg.projects:
            runs = _one(conn, "SELECT COUNT(*) FROM ci_run WHERE project = ?", p.name)
            jobs = _one(conn, "SELECT COUNT(*) FROM ci_job WHERE project = ?", p.name)
            builds = _one(conn, "SELECT COUNT(*) FROM gradle_build WHERE project = ?", p.name)
            latest = _one(conn, "SELECT MAX(created_at) FROM ci_run WHERE project = ?", p.name)
            print(f"{p.name:<16}{runs:>9,}{jobs:>9,}{builds:>8,}  {latest or '—'}")
    return 0


def cmd_install(args) -> int:
    home = config_mod.home_dir()
    home.mkdir(parents=True, exist_ok=True)

    cfg_path = config_mod.config_path(home)
    if cfg_path.exists():
        print(f"config already present: {cfg_path}")
    else:
        shutil.copyfile(_repo_root() / "config.example.toml", cfg_path)
        print(f"wrote starter config: {cfg_path}")
        print("  -> edit it before the first refresh")

    source = _repo_root() / "gradle" / INIT_SCRIPT_NAME
    target = _init_script_target()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() or target.is_symlink():
        print(f"gradle init script already installed: {target}")
    else:
        # Symlink rather than copy so `git pull` in this repo updates the
        # collector — there is no version to keep in sync by hand.
        target.symlink_to(source)
        print(f"linked gradle init script: {target} -> {source}")

    try:
        cfg = config_mod.load(home)
    except config_mod.ConfigError:
        return 0
    written = config_mod.write_gradle_projects(cfg)
    print(f"wrote project map: {written}")
    return 0


def cmd_uninstall(args) -> int:
    target = _init_script_target()
    if target.exists() or target.is_symlink():
        target.unlink()
        print(f"removed {target}")
    else:
        print(f"nothing to remove at {target}")
    print(f"data left intact at {config_mod.home_dir()} — delete it by hand if you want it gone")
    return 0


# --- helpers ----------------------------------------------------------------

def _init_script_target() -> Path:
    return Path.home() / ".gradle" / "init.d" / INIT_SCRIPT_NAME


def _one(conn, sql: str, *params):
    return conn.execute(sql, params).fetchone()[0]


def _size(path: Path) -> str:
    if not path.exists():
        return "not created yet"
    n = path.stat().st_size
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="buildloop",
        description="CI and local Gradle build trends, as a static file.",
    )
    parser.add_argument("--version", action="version", version=f"buildloop {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    refresh = sub.add_parser("refresh", help="collect data and regenerate dashboards")
    refresh.add_argument("projects", nargs="*", help="project names; default is all configured")
    refresh.add_argument("--open", action="store_true", help="open the first dashboard when done")
    refresh.set_defaults(func=cmd_refresh)

    status = sub.add_parser("status", help="show what is stored (no network)")
    status.set_defaults(func=cmd_status)

    install = sub.add_parser("install", help="create the config and link the Gradle init script")
    install.set_defaults(func=cmd_install)

    uninstall = sub.add_parser("uninstall", help="remove the Gradle init script")
    uninstall.set_defaults(func=cmd_uninstall)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except config_mod.ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    except gh.GhError as exc:
        print(f"github error: {exc}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
