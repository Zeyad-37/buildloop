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
 *      Gradle build on the machine — other projects, and every IDE sync. The
 *      gate keeps the dataset clean and keeps overhead at zero for untracked
 *      work.
 *   2. It never fails a build. Every collection path is wrapped; any error
 *      degrades to a debug log. A metrics tool that breaks the build has
 *      negative value.
 *
 * Configuration-cache compatible by construction: a BuildService receiving
 * TaskFinishEvents via BuildEventsListenerRegistry, plus a FlowAction for the
 * build-completion callback. `buildFinished` / BuildListener are NOT usable.
 */

import org.gradle.api.Plugin
import org.gradle.api.flow.FlowAction
import org.gradle.api.flow.FlowParameters
import org.gradle.api.flow.FlowProviders
import org.gradle.api.flow.FlowScope
import org.gradle.api.initialization.Settings
import org.gradle.api.provider.ListProperty
import org.gradle.api.provider.Property
import org.gradle.api.services.BuildService
import org.gradle.api.services.BuildServiceParameters
import org.gradle.api.services.ServiceReference
import org.gradle.api.tasks.Input
import org.gradle.build.event.BuildEventsListenerRegistry
import org.gradle.tooling.events.FinishEvent
import org.gradle.tooling.events.OperationCompletionListener
import org.gradle.tooling.events.task.TaskFinishEvent
import org.gradle.tooling.events.task.TaskSkippedResult
import org.gradle.tooling.events.task.TaskSuccessResult
import java.io.File
import java.io.FileOutputStream
import java.lang.management.ManagementFactory
import java.time.Instant
import java.time.temporal.ChronoUnit
import java.util.UUID
import java.util.concurrent.atomic.AtomicInteger
import javax.inject.Inject

// ---------------------------------------------------------------------------
// Task outcome accumulator
// ---------------------------------------------------------------------------

/**
 * Counts task outcomes for one build. `TaskSuccessResult` exposes `isFromCache`
 * and `isUpToDate`, which is where cache effectiveness comes from.
 *
 * The service is instantiated once per build, so its construction timestamp is
 * a usable proxy for the start of work — needed because on a configuration
 * cache HIT the configuration-time timestamp is a stale cached value.
 */
abstract class BuildLoopTaskStats :
    BuildService<BuildServiceParameters.None>, OperationCompletionListener {

    val serviceStartMs: Long = System.currentTimeMillis()

    /** Earliest task start / latest task end seen, for the execution span. */
    @Volatile var firstTaskStartMs: Long = Long.MAX_VALUE
        private set
    @Volatile var lastTaskEndMs: Long = Long.MIN_VALUE
        private set

    private val totalCount = AtomicInteger()
    private val executedCount = AtomicInteger()
    private val fromCacheCount = AtomicInteger()
    private val upToDateCount = AtomicInteger()

    val total: Int get() = totalCount.get()
    val executed: Int get() = executedCount.get()
    val fromCache: Int get() = fromCacheCount.get()
    val upToDate: Int get() = upToDateCount.get()

    override fun onFinish(event: FinishEvent) {
        if (event !is TaskFinishEvent) return
        try {
            totalCount.incrementAndGet()
            synchronized(this) {
                if (event.result.startTime < firstTaskStartMs) firstTaskStartMs = event.result.startTime
                if (event.result.endTime > lastTaskEndMs) lastTaskEndMs = event.result.endTime
            }
            when (val result = event.result) {
                is TaskSuccessResult -> when {
                    result.isFromCache -> fromCacheCount.incrementAndGet()
                    result.isUpToDate -> upToDateCount.incrementAndGet()
                    else -> executedCount.incrementAndGet()
                }
                is TaskSkippedResult -> Unit
                else -> executedCount.incrementAndGet() // failed tasks did run
            }
        } catch (ignored: Throwable) {
            // Never let accounting break a build.
        }
    }
}

// ---------------------------------------------------------------------------
// Build-completion writer
// ---------------------------------------------------------------------------

abstract class BuildLoopRecordAction : FlowAction<BuildLoopRecordAction.Params> {

    interface Params : FlowParameters {
        @get:Input val projectName: Property<String>
        @get:Input val tasks: ListProperty<String>
        @get:Input val configId: Property<String>
        @get:Input val buildStartMs: Property<Long>
        @get:Input val configCacheMode: Property<String>
        @get:Input val gradleVersion: Property<String>
        @get:Input val homeDir: Property<String>
        @get:Input val failed: Property<Boolean>

        @get:ServiceReference("buildloopTaskStats")
        val stats: Property<BuildLoopTaskStats>
    }

    override fun execute(parameters: Params) {
        try {
            write(parameters)
        } catch (ignored: Throwable) {
            // A metrics tool that breaks the build has negative value.
        }
    }

    private fun write(p: Params) {
        val now = System.currentTimeMillis()
        val home = File(p.homeDir.get())
        val stats = p.stats.orNull

        val configCache = detectConfigCache(home, p.configId.get(), p.configCacheMode.get())

        // What we can honestly measure depends on whether configuration ran.
        //
        // On a MISS, the init script's top-level code executed at the very
        // start of the build, so `buildStartMs` is near-exact — it covers
        // settings evaluation, configuration and execution.
        //
        // On a HIT, none of that ran: `buildStartMs` is a stale value restored
        // from the cache, and the earliest moment buildloop can observe is the
        // first task event. Configuration-cache *load* time (a few hundred ms
        // on a large build) precedes that and is not exposed by any public
        // Gradle API — `BuildEventsListenerRegistry` offers only
        // `onTaskCompletion`. So a hit build's duration is execution-only, and
        // says so via `measured_from` rather than quietly under-reporting into
        // the same series as a full build.
        val execSpanMs: Long? = stats
            ?.takeIf { it.total > 0 && it.lastTaskEndMs >= it.firstTaskStartMs }
            ?.let { it.lastTaskEndMs - it.firstTaskStartMs }

        val fullMeasurement = configCache != BuildLoopConfig.HIT
        val durationMs: Long? = when {
            fullMeasurement -> now - p.buildStartMs.get()
            stats == null -> null
            else -> now - minOf(stats.serviceStartMs, stats.firstTaskStartMs)
        }
        val measuredFrom = when {
            durationMs == null -> null
            fullMeasurement -> BuildLoopConfig.FROM_BUILD_START
            else -> BuildLoopConfig.FROM_EXECUTION
        }

        val uptimeMs = runCatching { ManagementFactory.getRuntimeMXBean().uptime }.getOrNull()
        val daemonReused: Boolean? =
            if (uptimeMs == null || durationMs == null) null
            else uptimeMs > durationMs + BuildLoopConfig.DAEMON_FRESH_SLACK_MS

        val json = buildString {
            append('{')
            appendField("ts", Instant.now().truncatedTo(ChronoUnit.SECONDS).toString())
            append(',')
            appendField("project", p.projectName.get())
            append(",\"tasks\":[")
            p.tasks.get().forEachIndexed { i, t ->
                if (i > 0) append(',')
                append('"').append(escape(t)).append('"')
            }
            append(']')
            append(",\"duration_ms\":").append(durationMs ?: "null")
            append(",\"measured_from\":").append(if (measuredFrom == null) "null" else "\"$measuredFrom\"")
            append(",\"exec_ms\":").append(execSpanMs ?: "null")
            append(',')
            appendField("outcome", if (p.failed.get()) "failed" else "success")
            append(",\"task_count\":").append(stats?.total ?: "null")
            append(",\"executed\":").append(stats?.executed ?: "null")
            append(",\"from_cache\":").append(stats?.fromCache ?: "null")
            append(",\"up_to_date\":").append(stats?.upToDate ?: "null")
            append(",\"config_cache\":").append(if (configCache == null) "null" else "\"$configCache\"")
            append(',')
            appendField("gradle_version", p.gradleVersion.get())
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
     * the registered FlowAction is restored and fires either way. So a config
     * id we have never seen before was generated this build (configuration ran
     * => miss); an id already on record was restored from a cache entry (hit).
     *
     * The seen-ids are kept as a SET rather than a single "last id", because a
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
    private val flowScope: FlowScope,
    private val flowProviders: FlowProviders,
    private val eventsRegistry: BuildEventsListenerRegistry,
) : Plugin<Settings> {

    override fun apply(settings: Settings) {
        val gradle = settings.gradle
        val home = BuildLoopConfig.home()
        val projectName = BuildLoopConfig.resolveProject(home, settings.rootProject.name) ?: return

        val statsService = gradle.sharedServices.registerIfAbsent(
            BuildLoopConfig.SERVICE_NAME, BuildLoopTaskStats::class.java
        ) {}
        eventsRegistry.onTaskCompletion(statsService)

        val configId = UUID.randomUUID().toString()
        // Set by the init script's top-level code, which runs before the
        // settings script — the earliest point buildloop can observe. Falls
        // back to now if absent, which only under-reports.
        val buildStartMs = (settings.extensions.extraProperties
            .takeIf { it.has(BuildLoopConfig.START_MS_KEY) }
            ?.get(BuildLoopConfig.START_MS_KEY) as? Long) ?: System.currentTimeMillis()
        val startParameter = gradle.startParameter

        flowScope.always(BuildLoopRecordAction::class.java) {
            parameters.projectName.set(projectName)
            parameters.tasks.set(startParameter.taskNames)
            parameters.configId.set(configId)
            parameters.buildStartMs.set(buildStartMs)
            parameters.configCacheMode.set(BuildLoopConfig.configCacheMode(startParameter))
            parameters.gradleVersion.set(gradle.gradleVersion)
            parameters.homeDir.set(home.absolutePath)
            parameters.failed.set(
                flowProviders.buildWorkResult.map { it.failure.isPresent }
            )
        }
    }
}

object BuildLoopConfig {
    const val SERVICE_NAME = "buildloopTaskStats"
    const val HIT = "hit"
    const val MISS = "miss"
    const val MAX_TRACKED_CONFIG_IDS = 200

    const val START_MS_KEY = "buildloop.buildStartMs"
    const val FROM_BUILD_START = "build_start"
    const val FROM_EXECUTION = "execution"

    const val CC_ENABLED = "enabled"
    const val CC_DISABLED = "disabled"
    const val CC_UNKNOWN = "unknown"

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

    /** A fresh daemon's first build has uptime ~= startup + build; a reused one has far more. */
    const val DAEMON_FRESH_SLACK_MS = 15_000L

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
