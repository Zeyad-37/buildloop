/*
 * buildloop — local Gradle build collector.
 *
 * Install: copy (or symlink) this file to ~/.gradle/init.d/buildloop.init.gradle.kts
 * Uninstall: delete it. Builds return to exactly prior behaviour on the next
 * invocation; no consumer repo's build files are ever modified.
 *
 * Two properties this script must have, or it becomes a liability:
 *
 *   1. It early-returns for unknown projects. init.d scripts run for EVERY
 *      Gradle build on the machine — other projects, included builds such as
 *      build-logic, and every IDE sync. The gate keeps the dataset clean and
 *      keeps overhead at zero for untracked work.
 *   2. It never fails a build. Every collection path is wrapped; any error
 *      degrades to a debug log. A metrics tool that breaks the build has
 *      negative value.
 *
 * Configuration-cache compatible by construction: a single BuildService
 * receiving TaskFinishEvents via BuildEventsListenerRegistry, which writes its
 * row when Gradle closes it at the end of the build. `buildFinished` and
 * BuildListener are NOT usable under the configuration cache.
 *
 * WHY NOT A FlowAction. FlowScope/FlowProviders is the documented
 * build-completion hook and gives an authoritative failure signal, so it was
 * the obvious choice. It does not survive contact with an init script: a
 * FlowAction that reaches the task-outcome BuildService through
 * `@ServiceReference` cannot be serialised into the configuration cache when
 * both classes are declared in a `.gradle.kts` init script —
 *
 *   Cannot set the value of a property of type BuildLoopCollector loaded with
 *   VisitableURLClassLoader(...buildloop.init.gradle.kts...) using a provider
 *   of type BuildLoopCollector loaded with VisitableURLClassLoader(same)
 *
 * — and Gradle recovers by configuring twice, leaving TWO action instances
 * that each write a row: one with task counts and one with nulls. Every build
 * double-counted, half the rows empty. `AutoCloseable.close()` on the service
 * itself is a build-completion hook with no cross-bean reference to serialise,
 * so it sidesteps the whole problem. The cost is the failure signal, which is
 * recovered from task results instead (see `anyTaskFailed`).
 */

import org.gradle.api.Plugin
import org.gradle.api.initialization.Settings
import org.gradle.api.provider.ListProperty
import org.gradle.api.provider.Property
import org.gradle.api.services.BuildService
import org.gradle.api.services.BuildServiceParameters
import org.gradle.build.event.BuildEventsListenerRegistry
import org.gradle.tooling.events.FinishEvent
import org.gradle.tooling.events.OperationCompletionListener
import org.gradle.tooling.events.task.TaskFailureResult
import org.gradle.tooling.events.task.TaskFinishEvent
import org.gradle.tooling.events.task.TaskSkippedResult
import org.gradle.tooling.events.task.TaskSuccessResult
import java.io.File
import java.io.FileOutputStream
import java.lang.management.ManagementFactory
import java.time.Instant
import java.time.temporal.ChronoUnit
import java.util.UUID
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger
import javax.inject.Inject

// ---------------------------------------------------------------------------
// Collector
// ---------------------------------------------------------------------------

abstract class BuildLoopCollector :
    BuildService<BuildLoopCollector.Params>, OperationCompletionListener, AutoCloseable {

    interface Params : BuildServiceParameters {
        val projectName: Property<String>
        val tasks: ListProperty<String>
        val configId: Property<String>
        val buildStartMs: Property<Long>
        val configCacheMode: Property<String>
        val gradleVersion: Property<String>
        val homeDir: Property<String>
    }

    /**
     * Instantiated once per build, when the first build event is delivered.
     * On a configuration-cache hit this is the earliest moment observable —
     * see the note on `measured_from` below.
     */
    private val serviceStartMs: Long = System.currentTimeMillis()

    private val total = AtomicInteger()
    private val executed = AtomicInteger()
    private val fromCache = AtomicInteger()
    private val upToDate = AtomicInteger()
    private val anyTaskFailed = AtomicBoolean(false)
    private val written = AtomicBoolean(false)

    @Volatile private var firstTaskStartMs: Long = Long.MAX_VALUE
    @Volatile private var lastTaskEndMs: Long = Long.MIN_VALUE

    override fun onFinish(event: FinishEvent) {
        if (event !is TaskFinishEvent) return
        try {
            total.incrementAndGet()
            synchronized(this) {
                if (event.result.startTime < firstTaskStartMs) firstTaskStartMs = event.result.startTime
                if (event.result.endTime > lastTaskEndMs) lastTaskEndMs = event.result.endTime
            }
            when (val result = event.result) {
                is TaskSuccessResult -> when {
                    result.isFromCache -> fromCache.incrementAndGet()
                    result.isUpToDate -> upToDate.incrementAndGet()
                    else -> executed.incrementAndGet()
                }
                is TaskSkippedResult -> Unit
                is TaskFailureResult -> {
                    anyTaskFailed.set(true)
                    executed.incrementAndGet() // a failed task did run
                }
                else -> executed.incrementAndGet()
            }
        } catch (ignored: Throwable) {
            // Never let accounting break a build.
        }
    }

    /** Gradle closes build services at the end of the build. This is the hook. */
    override fun close() {
        if (!written.compareAndSet(false, true)) return
        try {
            writeRow()
        } catch (ignored: Throwable) {
            // A metrics tool that breaks the build has negative value.
        }
    }

    private fun writeRow() {
        val now = System.currentTimeMillis()
        val home = File(parameters.homeDir.get())
        val configCache = detectConfigCache(home, parameters.configId.get(), parameters.configCacheMode.get())
        val taskCount = total.get()

        // Task-execution span. Unlike duration_ms this means the same thing on
        // every build, so it is the metric to reach for when comparing across
        // configuration-cache states.
        val execSpanMs: Long? =
            if (taskCount > 0 && lastTaskEndMs >= firstTaskStartMs) lastTaskEndMs - firstTaskStartMs else null

        // What we can honestly measure depends on whether configuration ran.
        //
        // On a MISS, the init script's top-level code executed at the very
        // start of the build, so `buildStartMs` is near-exact — it covers
        // settings evaluation, configuration and execution.
        //
        // On a HIT, none of that ran: `buildStartMs` is a stale value restored
        // from the cache, and the earliest moment buildloop can observe is this
        // service's own construction. Configuration-cache *load* time (a few
        // hundred ms on a large build) precedes it and is not exposed by any
        // public Gradle API. So a hit build's duration is execution-only, and
        // says so via `measured_from` rather than quietly under-reporting into
        // the same series as a full build.
        val fullMeasurement = configCache != BuildLoopConfig.HIT
        val durationMs: Long =
            if (fullMeasurement) now - parameters.buildStartMs.get()
            else now - minOf(serviceStartMs, firstTaskStartMs)
        val measuredFrom =
            if (fullMeasurement) BuildLoopConfig.FROM_BUILD_START else BuildLoopConfig.FROM_EXECUTION

        val uptimeMs = runCatching { ManagementFactory.getRuntimeMXBean().uptime }.getOrNull()
        val daemonReused: Boolean? =
            if (uptimeMs == null) null else uptimeMs > durationMs + BuildLoopConfig.DAEMON_FRESH_SLACK_MS

        val json = buildString {
            append('{')
            appendField("ts", Instant.now().truncatedTo(ChronoUnit.SECONDS).toString())
            append(',')
            appendField("project", parameters.projectName.get())
            append(",\"tasks\":[")
            parameters.tasks.get().forEachIndexed { i, t ->
                if (i > 0) append(',')
                append('"').append(escape(t)).append('"')
            }
            append(']')
            append(",\"duration_ms\":").append(durationMs)
            append(",\"measured_from\":\"").append(measuredFrom).append('"')
            append(",\"exec_ms\":").append(execSpanMs ?: "null")
            append(',')
            appendField("outcome", if (anyTaskFailed.get()) "failed" else "success")
            append(",\"task_count\":").append(taskCount)
            append(",\"executed\":").append(executed.get())
            append(",\"from_cache\":").append(fromCache.get())
            append(",\"up_to_date\":").append(upToDate.get())
            append(",\"config_cache\":").append(if (configCache == null) "null" else "\"$configCache\"")
            append(',')
            appendField("gradle_version", parameters.gradleVersion.get())
            append(",\"daemon_reused\":").append(daemonReused?.toString() ?: "null")
            append('}')
            append('\n')
        }

        // One write() call on an O_APPEND handle: concurrent builds interleave
        // whole lines rather than corrupting each other's.
        FileOutputStream(File(home, "builds.jsonl"), true).use {
            it.write(json.toByteArray(Charsets.UTF_8))
        }
    }

    /**
     * Configuration-cache hit/miss, by heuristic.
     *
     * An init script's *configuration* phase only runs on a cache miss, while
     * the registered build service is restored from the cache and instantiated
     * either way. So a config id we have never seen before was generated this
     * build (configuration ran => miss); an id already on record was restored
     * from a cache entry (=> hit).
     *
     * The seen ids are kept as a SET rather than a single "last id", because a
     * single slot is wrong the moment a project has more than one cache entry:
     * alternating `assembleDebug` and `test` builds would each see the other's
     * id and report a miss on every build.
     *
     * This is a heuristic, not a first-party API. If Gradle's behaviour changes
     * under a future upgrade it must degrade to null, never to a wrong answer.
     */
    private fun detectConfigCache(home: File, configId: String, mode: String): String? {
        // "disabled" -> configuration always runs, there is no hit/miss to report.
        // "unknown"  -> Gradle's API moved under us; degrade rather than guess.
        if (mode != BuildLoopConfig.CC_ENABLED) return null
        return try {
            val file = File(home, "config-ids")
            val seen = if (file.isFile) file.readLines().filter { it.isNotBlank() } else emptyList()
            if (configId in seen) {
                BuildLoopConfig.HIT
            } else {
                val kept = (seen + configId).takeLast(BuildLoopConfig.MAX_TRACKED_CONFIG_IDS)
                file.writeText(kept.joinToString("\n", postfix = "\n"))
                BuildLoopConfig.MISS
            }
        } catch (ignored: Throwable) {
            null
        }
    }

    private fun StringBuilder.appendField(key: String, value: String) {
        append('"').append(key).append("\":\"").append(escape(value)).append('"')
    }

    private fun escape(s: String): String = buildString(s.length) {
        for (c in s) when {
            c == '"' -> append("\\\"")
            c == '\\' -> append("\\\\")
            c == '\n' -> append("\\n")
            c == '\r' -> append("\\r")
            c == '\t' -> append("\\t")
            c < ' ' -> append(String.format("\\u%04x", c.code))
            else -> append(c)
        }
    }
}

// ---------------------------------------------------------------------------
// Wiring
// ---------------------------------------------------------------------------

abstract class BuildLoopPlugin @Inject constructor(
    private val eventsRegistry: BuildEventsListenerRegistry,
) : Plugin<Settings> {

    override fun apply(settings: Settings) {
        val gradle = settings.gradle
        val home = BuildLoopConfig.home()
        val projectName = BuildLoopConfig.resolveProject(home, settings.rootProject.name) ?: return

        // Set by the init script's top-level code, which runs before the
        // settings script — the earliest point buildloop can observe. Falls
        // back to now if absent, which only under-reports.
        val buildStartMs = (settings.extensions.extraProperties
            .takeIf { it.has(BuildLoopConfig.START_MS_KEY) }
            ?.get(BuildLoopConfig.START_MS_KEY) as? Long) ?: System.currentTimeMillis()
        val startParameter = gradle.startParameter

        val collector = gradle.sharedServices.registerIfAbsent(
            BuildLoopConfig.SERVICE_NAME, BuildLoopCollector::class.java
        ) {
            parameters.projectName.set(projectName)
            parameters.tasks.set(startParameter.taskNames)
            parameters.configId.set(UUID.randomUUID().toString())
            parameters.buildStartMs.set(buildStartMs)
            parameters.configCacheMode.set(BuildLoopConfig.configCacheMode(startParameter))
            parameters.gradleVersion.set(gradle.gradleVersion)
            parameters.homeDir.set(home.absolutePath)
        }
        eventsRegistry.onTaskCompletion(collector)
    }
}

object BuildLoopConfig {
    const val SERVICE_NAME = "buildloopCollector"
    const val HIT = "hit"
    const val MISS = "miss"
    const val MAX_TRACKED_CONFIG_IDS = 200

    const val START_MS_KEY = "buildloop.buildStartMs"
    const val FROM_BUILD_START = "build_start"
    const val FROM_EXECUTION = "execution"

    const val CC_ENABLED = "enabled"
    const val CC_DISABLED = "disabled"
    const val CC_UNKNOWN = "unknown"

    /** A fresh daemon's first build has uptime ~= startup + build; a reused one has far more. */
    const val DAEMON_FRESH_SLACK_MS = 15_000L

    /**
     * Is the configuration cache on for this build?
     *
     * Read reflectively on purpose. `StartParameter.isConfigurationCacheRequested`
     * is deprecated in Gradle 9 and removed in 10, and a direct call emits a
     * deprecation warning into *every build of every tracked project* — which
     * would land in consumers' own build-warning reports. Its replacement
     * (`getConfigurationCache()`) returns an internal `Option.Value` type that
     * must not be compiled against. Reflection avoids both problems and, when
     * the API next moves, degrades to "unknown" -> a null metric rather than a
     * wrong one.
     */
    fun configCacheMode(startParameter: Any): String {
        for (accessor in listOf("getConfigurationCache", "isConfigurationCacheRequested")) {
            val enabled = try {
                val raw = startParameter.javaClass.getMethod(accessor).invoke(startParameter)
                when (raw) {
                    is Boolean -> raw
                    null -> null
                    else -> raw.javaClass.methods
                        .firstOrNull { it.name == "get" && it.parameterCount == 0 }
                        ?.also { it.isAccessible = true }
                        ?.invoke(raw) as? Boolean
                }
            } catch (ignored: Throwable) {
                null
            }
            if (enabled != null) return if (enabled) CC_ENABLED else CC_DISABLED
        }
        return CC_UNKNOWN
    }

    /**
     * Match `rootProject.name` against the tracked-project list.
     *
     * The list is a two-column TSV rather than the TOML config because an init
     * script cannot parse TOML without adding a dependency. `buildloop refresh`
     * regenerates it from the config on every run, so it cannot drift.
     */
    fun resolveProject(home: File, rootProjectName: String): String? = try {
        val mapping = File(home, "gradle-projects")
        if (!mapping.isFile) {
            null
        } else {
            mapping.readLines()
                .asSequence()
                .mapNotNull { line ->
                    val parts = line.split('\t')
                    if (parts.size >= 2 && parts[0].trim() == rootProjectName) parts[1].trim() else null
                }
                .firstOrNull()
        }
    } catch (ignored: Throwable) {
        null
    }

    fun home(): File {
        val override = System.getenv("BUILDLOOP_HOME")
        return if (!override.isNullOrBlank()) File(override)
        else File(System.getProperty("user.home"), ".buildloop")
    }
}

// ---------------------------------------------------------------------------

// Captured here, at the top level of the init script, because init scripts run
// before the settings script — and on a large project settings evaluation alone
// is seconds. Taking the timestamp inside `settingsEvaluated` would silently
// exclude it (measured: 5.0s real vs 1.8s observed on Steady).
val buildLoopStartMs = System.currentTimeMillis()

if (System.getenv("BUILDLOOP_DISABLE").isNullOrBlank()) {
    gradle.settingsEvaluated {
        try {
            extensions.extraProperties.set(BuildLoopConfig.START_MS_KEY, buildLoopStartMs)
            pluginManager.apply(BuildLoopPlugin::class.java)
        } catch (t: Throwable) {
            logger.debug("buildloop: disabled for this build ({})", t.toString())
        }
    }
}
