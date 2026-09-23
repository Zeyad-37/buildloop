"""Say *why* a CI job failed, from its log.

The Actions API reports that a job failed and which step, but not what went
wrong. Check-run annotations look like the answer and are not: for a failing
Gradle or shell step the only annotation is "Process completed with exit code
1." The reason is in the log, so that is what this reads.

Two things make a raw log misleading, and both are handled here:

* **Other steps' errors.** A ``continue-on-error`` step earlier in the job can
  print ``##[error]`` or a whole Gradle failure block and not fail the job.
  Only lines inside the failing step's own time window are considered.
* **Specificity.** One failure prints several layers of message — a test
  name, then "Execution failed for task ':app:test'", then "Process completed
  with exit code 1." — and the outermost is the least useful. Extractors run
  most-specific first and the first to find something wins.

Everything here is pure: no network, no database. The collector feeds it a
job payload and log text; the dashboard reads what it returns.
"""

from __future__ import annotations

import re

MAX_REASON_CHARS = 220
MAX_EXCERPT_LINES = 24
MAX_LINE_CHARS = 240

# "2026-09-09T16:50:34.9566224Z " — every line GitHub stores carries one.
_TS = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.\d+)?Z ?")
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_EXIT_CODE = re.compile(r"^##\[error\]Process completed with exit code (\d+)\.?$")

_TEST_FAILED = re.compile(r"^(?!> Task )(\S.*? > .+) FAILED$")
_KOTLIN_ERROR = re.compile(r"^e: (.+)$")
_GRADLE_BLOCK = "* What went wrong:"
_XCODE_FAILED = "The following build commands failed:"
_GH_ERROR = re.compile(r"^##\[error\](.+)$")
_TOOL_ERROR = re.compile(r"^(?:error|fatal|Error|ERROR|FATAL)(?:\[\w+\])?: (.+)$")

# A runner-local path tells you nothing a file name doesn't, and keeps the
# reason unreadably long.
_PATH = re.compile(r"(?:file://)?(?<![\w.~-])(?:/[\w.@+-]+){2,}/([\w.@+-]+)")


# --- log shaping ------------------------------------------------------------

def _lines(log: str) -> list[tuple[str | None, str]]:
    """(second-resolution timestamp, text) per line, ANSI stripped."""
    out = []
    last_ts = None
    for raw in log.splitlines():
        m = _TS.match(raw)
        if m:
            last_ts = m.group(1)
            raw = raw[m.end():]
        out.append((last_ts, _ANSI.sub("", raw).rstrip()))
    return out


def _second(iso: str | None) -> str | None:
    return iso[:19] if iso and len(iso) >= 19 else None


def failing_step(job: dict) -> dict | None:
    for step in job.get("steps") or []:
        if step.get("conclusion") == "failure":
            return step
    return None


def _scope(lines, step: dict | None) -> list[str]:
    """The failing step's lines, or the whole log if they can't be isolated.

    The API gives step bounds to the second and the log gives every line a
    sub-second stamp, so comparing both at second resolution keeps the whole
    of the step's first and last second.
    """
    start = _second(step.get("started_at")) if step else None
    end = _second(step.get("completed_at")) if step else None
    if start and end:
        scoped = [text for ts, text in lines if ts and start <= ts <= end]
        if any(t.strip() for t in scoped):
            return scoped
    return [text for _, text in lines]


def _short(text: str) -> str:
    text = _PATH.sub(r"\1", text.strip())
    return text if len(text) <= MAX_REASON_CHARS else text[: MAX_REASON_CHARS - 1] + "…"


def _clip(text: str) -> str:
    return text if len(text) <= MAX_LINE_CHARS else text[: MAX_LINE_CHARS - 1] + "…"


# --- extractors, most specific first ----------------------------------------
# Each returns (reason, excerpt lines) or None.

def _failed_tests(lines):
    hits = [i for i, t in enumerate(lines) if _TEST_FAILED.match(t)]
    if not hits:
        return None
    names = [_TEST_FAILED.match(lines[i]).group(1).strip() for i in hits]
    excerpt = []
    for i in hits:
        excerpt.append(lines[i])
        # Gradle prints the assertion on the next, indented line.
        if i + 1 < len(lines) and lines[i + 1].startswith((" ", "\t")) and lines[i + 1].strip():
            excerpt.append(lines[i + 1])
    excerpt += [t for t in lines if re.search(r"\d+ tests? completed, \d+ failed", t)]
    more = f" (+{len(names) - 1} more)" if len(names) > 1 else ""
    return f"Test failed: {names[0]}{more}", excerpt


def _kotlin_errors(lines):
    errors = [t for t in lines if _KOTLIN_ERROR.match(t)]
    if not errors:
        return None
    first = _KOTLIN_ERROR.match(errors[0]).group(1)
    more = f" (+{len(errors) - 1} more)" if len(errors) > 1 else ""
    return f"Compile error: {first}{more}", errors


def _gradle_block(lines):
    """Gradle's own summary: headline, then the innermost '>' cause."""
    try:
        start = next(i for i, t in enumerate(lines) if t.strip() == _GRADLE_BLOCK)
    except StopIteration:
        return None
    block = []
    for t in lines[start + 1:]:
        if t.startswith("* ") or re.match(r"^\d+: Task failed", t):
            break
        if t.strip() and not t.startswith("[Incubating]"):
            block.append(t)
    if not block:
        return None
    headline = re.sub(r" \((?:registered|registered in|registered by) [^)]*\)", "", block[0]).rstrip(".")
    cause = None
    for t in block[1:]:
        s = t.strip()
        if s.startswith(">"):
            cause = s.lstrip("> ").strip()
        elif s.startswith("- "):
            continue  # a sibling problem list Gradle interleaves into the chain
        elif cause:
            break
    reason = f"{headline} — {cause}" if cause else headline
    return reason, [_GRADLE_BLOCK] + block


def _xcode(lines):
    try:
        start = next(i for i, t in enumerate(lines) if t.strip() == _XCODE_FAILED)
    except StopIteration:
        return None
    failed = []
    for t in lines[start + 1:]:
        if not t.strip() or re.match(r"^\(\d+ failures?\)", t.strip()):
            break
        failed.append(t.strip())
    if not failed:
        return None
    return f"xcodebuild: {failed[0]}", [_XCODE_FAILED] + failed


def _lead_in(lines, index: int, n: int = 8) -> list[str]:
    """The output just before an error: a script usually explains itself there.

    Collapsed ``##[group]`` sections are left out — the runner uses them for
    the step's own header (the command, its shell and env), which is never
    the explanation. A ``##[group]Run`` header starts a step, so nothing
    before the last one is this step's output: the time window can include
    the end of the previous step when both finished in the same second.
    """
    out, grouped = [], False
    for t in lines[:index]:
        if t.startswith("##[group]Run "):
            out, grouped = [], True
        elif t.startswith("##[group]"):
            grouped = True
        elif t.startswith("##[endgroup]"):
            grouped = False
        elif not grouped and t.strip() and not t.startswith("##["):
            out.append(t)
    return out[-n:]


def _gh_errors(lines):
    hits = [i for i, t in enumerate(lines) if _GH_ERROR.match(t) and not _EXIT_CODE.match(t)]
    if not hits:
        return None
    return (_GH_ERROR.match(lines[hits[0]]).group(1),
            _lead_in(lines, hits[0]) + [lines[i] for i in hits])


def _tool_errors(lines):
    hits = [i for i, t in enumerate(lines) if _TOOL_ERROR.match(t)]
    if not hits:
        return None
    return lines[hits[0]], _lead_in(lines, hits[0], 4) + [lines[i] for i in hits]


def _tail(lines):
    """Nothing recognisable: say how it exited and show what came just before."""
    code = None
    cut = len(lines)
    for i, t in enumerate(lines):
        m = _EXIT_CODE.match(t)
        if m:
            code, cut = m.group(1), i
            break
    before = _lead_in(lines, cut, 10)
    last = before[-1].strip() if before else None
    if code and last:
        return f"Exit code {code} after: {last}", before
    if code:
        return f"Exit code {code}", before
    if last:
        return last, before
    return None


_EXTRACTORS = (_failed_tests, _kotlin_errors, _gradle_block, _xcode, _gh_errors, _tool_errors, _tail)


# --- entry points -----------------------------------------------------------

def diagnose(job: dict, log: str | None) -> dict:
    """{step, reason, signature, excerpt} for one failed job.

    ``log`` is None when GitHub no longer has it; the failing step is still
    worth recording even without a reason.
    """
    step = failing_step(job)
    step_name = step.get("name") if step else None
    if log is None:
        return {"step": step_name, "reason": None, "signature": None, "excerpt": None}

    lines = _scope(_lines(log), step)
    for extract in _EXTRACTORS:
        found = extract(lines)
        if found:
            reason, excerpt = found
            reason = _short(reason)
            return {
                "step": step_name,
                "reason": reason,
                "signature": signature(reason),
                "excerpt": "\n".join(_clip(t) for t in excerpt[:MAX_EXCERPT_LINES]),
            }
    return {"step": step_name, "reason": None, "signature": None, "excerpt": None}


_SIG_HEX = re.compile(r"\b[0-9a-f]{7,40}\b")
_SIG_NUM = re.compile(r"\d+(?:[.,]\d+)*")


def signature(reason: str) -> str:
    """Grouping key: the same failure with different numbers is one failure.

    "Line coverage regressed: 9842 covered lines < 9868" and "... 9901 < 9914"
    are the same problem recurring, and counting them separately would hide
    exactly the pattern the table exists to show. Commit SHAs likewise.
    """
    s = _SIG_HEX.sub("<sha>", reason)
    s = _SIG_NUM.sub("#", s)
    return s.lower()
