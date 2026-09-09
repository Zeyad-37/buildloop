"""Thin wrapper over the ``gh`` CLI.

Shelling out to ``gh`` rather than talking to ``api.github.com`` directly is
deliberate (RFC T-055 §7): it reuses the existing keychain-stored token, so
buildloop never reads, copies, or stores a credential. There is no new secret
to manage or leak, which is why the repo can be public without ceremony.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from urllib.parse import urlencode


class GhError(Exception):
    """A ``gh api`` call failed."""


class GhNotFound(GhError):
    """The requested resource does not exist (404)."""


def ensure_available() -> None:
    if shutil.which("gh") is None:
        raise GhError(
            "the 'gh' CLI is not on PATH — install it (brew install gh) and run 'gh auth login'"
        )


def api(path: str, params: dict | None = None, *, retries: int = 3) -> dict | list:
    """GET a REST path via ``gh api`` and return the parsed JSON.

    Retries transient failures (5xx, rate-limit, network) with backoff. A 404
    raises :class:`GhNotFound` immediately — retrying a missing resource is
    just a slower failure.
    """
    url = path if not params else f"{path}?{urlencode(params)}"
    last: Exception | None = None
    for attempt in range(retries):
        proc = subprocess.run(
            ["gh", "api", "-H", "Accept: application/vnd.github+json", url],
            capture_output=True,
            text=True,
        )
        if proc.returncode == 0:
            try:
                return json.loads(proc.stdout)
            except json.JSONDecodeError as exc:  # pragma: no cover - gh always emits JSON
                raise GhError(f"{url}: response was not JSON: {exc}") from exc

        stderr = (proc.stderr or "").strip()
        if "404" in stderr or "Not Found" in stderr:
            raise GhNotFound(f"{url}: not found")
        last = GhError(f"{url}: {stderr or f'gh exited {proc.returncode}'}")
        if attempt < retries - 1:
            time.sleep(2 ** attempt)
    raise last  # type: ignore[misc]


def rate_limit_remaining() -> int | None:
    """Core rate-limit budget left this hour, or None if it can't be read."""
    try:
        data = api("rate_limit")
    except GhError:
        return None
    try:
        return int(data["resources"]["core"]["remaining"])  # type: ignore[index]
    except (KeyError, TypeError, ValueError):
        return None
