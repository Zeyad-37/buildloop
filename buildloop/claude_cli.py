"""The one place buildloop talks to Claude.

Shells out to the `claude` CLI the way the CI collector shells out to `gh`: the
user's existing login does the authenticating, so buildloop handles no API key
and adds no Python dependency.

Every call runs with **no tools and no MCP servers**. The prompts carry data
that originated outside this machine — branch names, workflow, job and step
names from GitHub — and a model that can only produce text can at worst be
talked into a wrong answer, which the page renders escaped. A model that can
run a shell can be talked into anything.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile

DEFAULT_TIMEOUT = 240

_FLAGS = [
    "--tools", "",               # no built-in tools at all
    "--strict-mcp-config",       # and no MCP servers from any settings file
    "--no-session-persistence",  # don't litter ~/.claude with one-shot sessions
]


class ClaudeUnavailable(Exception):
    """Claude could not produce an answer. Callers degrade; they never crash."""


def available() -> bool:
    return shutil.which("claude") is not None


def command(prompt: str) -> list[str]:
    return ["claude", "-p", *_FLAGS, prompt]


def run(prompt: str, timeout: int = DEFAULT_TIMEOUT) -> str:
    if not available():
        raise ClaudeUnavailable("'claude' is not on PATH")
    # A scratch working directory: `claude` reads project settings and
    # CLAUDE.md from its cwd, and a consumer repo's instructions have nothing
    # to do with reading build numbers.
    with tempfile.TemporaryDirectory() as cwd:
        try:
            proc = subprocess.run(
                command(prompt), capture_output=True, text=True, timeout=timeout, cwd=cwd,
            )
        except subprocess.TimeoutExpired as exc:
            raise ClaudeUnavailable(f"claude timed out after {timeout}s") from exc
        except OSError as exc:
            raise ClaudeUnavailable(str(exc)) from exc
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip()[:200]
        raise ClaudeUnavailable(detail or f"claude exited {proc.returncode}")
    text = (proc.stdout or "").strip()
    if not text:
        raise ClaudeUnavailable("claude returned nothing")
    return text
