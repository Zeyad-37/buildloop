# Gradle init script — verification

The Python side is covered by unit tests. The init script is not: it runs
inside every local Gradle build, where a mistake degrades daily work, so it
gets a manual matrix instead.

Run against a scratch project plus one real project. Set `BUILDLOOP_HOME` to a
throwaway directory so you are not polluting real history.

```bash
export BUILDLOOP_HOME=/tmp/bl-verify
mkdir -p "$BUILDLOOP_HOME"
printf 'YourRootProjectName\tscratch\n' > "$BUILDLOOP_HOME/gradle-projects"
INIT=$PWD/gradle/buildloop.init.gradle.kts
./gradlew -I "$INIT" <task>
```

## Matrix

Last run: **2026-09-09**, Gradle 9.7.1, macOS, against a scratch project and
Steady (60-module KMP, configuration cache + build cache + parallel on).

| # | Scenario | Expected | Result |
|---|---|---|---|
| 1 | Configuration-cache miss | Row written, `config_cache: "miss"` | pass |
| 2 | Configuration-cache hit | Row written, `config_cache: "hit"` | pass |
| 3 | Alternating task sets (`hello`, `help`, `hello`) | Each returns to its own cache entry and reports `hit` | pass |
| 4 | `--no-configuration-cache` | Row written, `config_cache: null` | pass |
| 5 | `--no-build-cache` | Row written, `from_cache: 0` | pass |
| 6 | Failing build | Row written, `outcome: "failed"` | pass |
| 7 | Interrupted build (Ctrl-C) | No corrupt JSONL row; ingest tolerates truncation | pass (ingest covered by `test_gradle_ingest.py`) |
| 8 | Untracked project | No-op, no row, zero overhead | pass |
| 9 | Included build (`build-logic`) | No row — the gate matches only the outer `rootProject.name` | pass |
| 10 | IDE sync of a tracked project | Row written, `tasks` empty, stored as `(sync)` | **not yet verified** — needs a real Android Studio sync |

Case 3 is not in the original RFC and is the reason the config-cache detection
keeps a *set* of seen ids rather than a single "last id". With one slot,
alternating between two task sets makes every build look like a miss, because
each build sees the other's id. See below.

## Hard gate: overhead

> If it costs more than a low-single-digit number of milliseconds, it does not
> ship.

Measured on Steady, `./gradlew help`, configuration cache warm, five
consecutive runs each way:

| | runs (ms) | mean |
|---|---|---|
| without init script | 398, 406, 442, 448, 438 | 426 ms |
| with init script | 430, 408, 406, 398 | 410 ms |

Indistinguishable from run-to-run noise. **Gate passed.**

(The first "with" run is excluded: alternating between two `-I` arguments
changes the configuration-cache key, and Gradle keeps one entry per key by
default, so each switch forces a miss. That is an artefact of the measurement,
not of the script.)

## Config-cache detection, and why it is a heuristic

Gradle exposes no API for "was this build a configuration-cache hit". The
mechanism exploits the thing that makes it awkward: an init script's
*configuration* phase only runs on a miss, while the registered `FlowAction` is
restored from the cache and fires either way.

1. At configuration time, generate a UUID and capture it into the action's
   parameters.
2. At build completion, check it against the ids recorded by previous builds.
3. An id we have never seen was generated this build (configuration ran →
   **miss**). An id already on record was restored from a cache entry
   (**hit**).

Because this is a heuristic and not a contract, it must degrade to `null`
rather than to a wrong answer if Gradle's behaviour changes. It does, in three
places: an unreadable id file, a `--no-configuration-cache` build, and a
`StartParameter` whose configuration-cache accessor has moved (read
reflectively, precisely so a future removal yields "unknown" instead of a
compile failure or a false reading).

## What `duration_ms` actually measures

Read `measured_from` alongside it. There are two cases, and they are different
populations — the dashboard plots them as separate series for that reason.

| `measured_from` | Covers | Excludes |
|---|---|---|
| `build_start` | Settings evaluation, configuration, execution | Daemon/JVM startup, script compilation, configuration-cache *store* |
| `execution` | Task execution only | Everything before the first task event, including configuration-cache *load* |

On a configuration-cache hit there is no earlier hook to attach to:
`BuildEventsListenerRegistry` offers only `onTaskCompletion`, and the
configuration phase — where a build service could otherwise be created early —
did not run. Measured on Steady: Gradle reported `411ms` for a hit build of
which buildloop can observe `3ms`; the rest is cache load.

`exec_ms` (task-execution span) is comparable across every build and is the
metric to use when you need one number that means the same thing everywhere.

The alternative — reporting a single `duration_ms` across both cases — would
show a large "speedup" whenever the configuration cache started hitting, which
is a change in what was measured rather than in how long anything took.
