"""Configuration model and loader.

One TOML file at ``$BUILDLOOP_HOME/config.toml`` (default ``~/.buildloop``)
describes the projects to track. Adding a project is three lines; there is no
project-management UI and there will not be one (RFC T-055 §3).
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

DEFAULT_HOME = Path.home() / ".buildloop"

#: Sidecar file the Gradle init script reads. TOML parsing is not available to
#: an init script without adding a dependency, so the CLI projects the config
#: down to a two-column TSV that trivial Kotlin can parse.
GRADLE_PROJECTS_FILENAME = "gradle-projects"


class ConfigError(Exception):
    """Raised when the config file is missing or invalid."""


@dataclass(frozen=True)
class Project:
    """A single tracked project.

    At least one of ``github_repo`` / ``gradle_root`` must be set, otherwise
    there is nothing to collect.
    """

    name: str
    github_repo: str | None = None
    gradle_root: str | None = None

    @property
    def tracks_ci(self) -> bool:
        return self.github_repo is not None

    @property
    def tracks_gradle(self) -> bool:
        return self.gradle_root is not None


@dataclass(frozen=True)
class Config:
    projects: tuple[Project, ...]
    home: Path

    @property
    def db_path(self) -> Path:
        return self.home / "buildloop.sqlite"

    @property
    def jsonl_path(self) -> Path:
        return self.home / "builds.jsonl"

    @property
    def gradle_projects_path(self) -> Path:
        return self.home / GRADLE_PROJECTS_FILENAME

    def project(self, name: str) -> Project:
        for p in self.projects:
            if p.name == name:
                return p
        known = ", ".join(p.name for p in self.projects) or "<none>"
        raise ConfigError(f"unknown project {name!r}; configured: {known}")


def home_dir() -> Path:
    """Data directory. Overridable for tests via ``BUILDLOOP_HOME``."""
    override = os.environ.get("BUILDLOOP_HOME")
    return Path(override).expanduser() if override else DEFAULT_HOME


def config_path(home: Path | None = None) -> Path:
    return (home or home_dir()) / "config.toml"


def parse(raw: dict, home: Path) -> Config:
    """Validate a parsed TOML mapping into a :class:`Config`."""
    entries = raw.get("project")
    if not entries:
        raise ConfigError("config has no [[project]] entries")
    if not isinstance(entries, list):
        raise ConfigError("[[project]] must be an array of tables")

    projects: list[Project] = []
    seen: set[str] = set()
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ConfigError(f"[[project]] #{i + 1} is not a table")
        name = entry.get("name")
        if not name or not isinstance(name, str):
            raise ConfigError(f"[[project]] #{i + 1} is missing a string 'name'")
        if name in seen:
            raise ConfigError(f"duplicate project name {name!r}")
        seen.add(name)

        github_repo = entry.get("github_repo")
        gradle_root = entry.get("gradle_root")
        if github_repo is not None and not _is_repo_slug(github_repo):
            raise ConfigError(
                f"project {name!r}: github_repo must look like 'owner/repo', got {github_repo!r}"
            )
        if gradle_root is not None and not isinstance(gradle_root, str):
            raise ConfigError(f"project {name!r}: gradle_root must be a string")
        if github_repo is None and gradle_root is None:
            raise ConfigError(
                f"project {name!r} sets neither github_repo nor gradle_root — nothing to collect"
            )
        projects.append(Project(name=name, github_repo=github_repo, gradle_root=gradle_root))

    _reject_duplicate(projects, "gradle_root", lambda p: p.gradle_root)
    _reject_duplicate(projects, "github_repo", lambda p: p.github_repo)
    return Config(projects=tuple(projects), home=home)


def _reject_duplicate(projects, label, key) -> None:
    seen: dict[str, str] = {}
    for p in projects:
        value = key(p)
        if value is None:
            continue
        if value in seen:
            raise ConfigError(
                f"{label} {value!r} is claimed by both {seen[value]!r} and {p.name!r}"
            )
        seen[value] = p.name


def _is_repo_slug(value) -> bool:
    if not isinstance(value, str):
        return False
    parts = value.split("/")
    return len(parts) == 2 and all(parts) and " " not in value


def load(home: Path | None = None) -> Config:
    home = home or home_dir()
    path = config_path(home)
    if not path.exists():
        raise ConfigError(
            f"no config at {path}\n"
            f"Create it — see config.example.toml, or run: buildloop install"
        )
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from exc
    return parse(raw, home)


def write_gradle_projects(config: Config) -> Path:
    """Project the config down to the TSV the Gradle init script reads.

    Written on every refresh so the init script can never drift from the config.
    """
    lines = [
        f"{p.gradle_root}\t{p.name}"
        for p in config.projects
        if p.tracks_gradle
    ]
    path = config.gradle_projects_path
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    tmp.replace(path)
    return path
