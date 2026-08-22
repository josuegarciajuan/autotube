/** Floating bottom progress bar(s) for video generation — visible from any page.
 *  v2.5: Supports multiple concurrent jobs with stacked bars.
 */

import { useState, useRef, useEffect } from 'react'
import { useGeneration, type ActiveJob } from '../context/GenerationContext'
import { useGenerationProgress, type ProgressData } from '../hooks/useWebSocket'
import { api } from '../lib/api'
import { X, ChevronDown, ChevronUp, Loader2, CheckCircle, AlertCircle, Wand2, Layers, Ban, Upload, Globe, Hourglass } from 'lucide-react'

/** Human-readable label for each job action type. */
function actionLabel(action: string): string {
  switch (action) {
    case 'upload_only':       return 'Subir video'
    case 'publish':           return 'Publicar video'
    case 'generate_only':     return 'Generar (local)'
    case 'generate_and_upload': return 'Generar y Subir'
    case 'reassemble':        return 'Re-ensamblar'
    case 'regenerate_media':  return 'Regenerar media'
    case 'generate':          return 'Generar'
    default:                  return action || 'Pipeline'
  }
}

/** Icon component for each job action type. */
function actionIcon(action: string, size: number, className: string) {
  switch (action) {
    case 'upload_only': return <Upload size={size} className={className} />
    case 'publish':     return <Globe size={size} className={className} />
    default:            return <Wand2 size={size} className={className} />
  }
}

/** Individual progress bar for a single job.
 *  Must be a separate component so useGenerationProgress hook works per jobId. */
function JobProgressSlot({ job, onDismiss }: { job: ActiveJob; onDismiss: () => void }) {
  const [minimized, setMinimized] = useState(false)
  const [dismissed, setDismissed] = useState(false)
  const [cancelling, setCancelling] = useState(false)
  const { progress, connected } = useGenerationProgress(job.jobId)
  const dismissTimerRef = useRef<ReturnType<typeof setTimeout>>()
  const stuckTimerRef = useRef<ReturnType<typeof setTimeout>>()
  const stuckDismissedRef = useRef(false)

  const isCompleted = progress?.status === 'completed'
  const isFailed = progress?.status === 'failed'
  const isQueued = job.status === 'queued'
  const pct = progress?.progress ?? 0

  // ── Stuck detection: if no progress after 45s disconnected, auto-dismiss ──
  // Queued jobs are legitimately waiting for the queue consumer — never treat
  // them as stuck.
  useEffect(() => {
    const isStuck = !isQueued && !connected && pct === 0 && !isCompleted && !isFailed && !cancelling

    if (isStuck && !stuckTimerRef.current && !stuckDismissedRef.current) {
      stuckTimerRef.current = setTimeout(() => {
        stuckDismissedRef.current = true
        onDismiss() // zombie bar — kill it
      }, 45_000) // 45 seconds of no activity = stale
    } else if (!isStuck && stuckTimerRef.current) {
      clearTimeout(stuckTimerRef.current)
      stuckTimerRef.current = undefined
    }

    return () => {
      if (stuckTimerRef.current) {
        clearTimeout(stuckTimerRef.current)
        stuckTimerRef.current = undefined
      }
    }
  }, [isQueued, connected, pct, isCompleted, isFailed, cancelling, onDismiss])


  // Cancel this job: sends cancel request to backend + cleans up
  async function handleCancel() {
    setCancelling(true)
    try {
      await api.cancelJob(job.jobId)
    } catch {
      // Even if the API call fails, dismiss the bar
    }
    // Auto-dismiss after brief delay to show "Cancelando..." feedback
    setTimeout(() => onDismiss(), 2000)
  }

  // Auto-dismiss after 3s when done (cleanly with cleanup)
  useEffect(() => {
    if (isCompleted || isFailed) {
      if (!dismissTimerRef.current) {
        dismissTimerRef.current = setTimeout(() => {
          onDismiss()
        }, 3000)
      }
    }
    return () => {
      if (dismissTimerRef.current) {
        clearTimeout(dismissTimerRef.current)
        dismissTimerRef.current = undefined
      }
    }
  }, [isCompleted, isFailed, onDismiss])

  if (dismissed) {
    return null
  }

  return (
    <div className="border-t border-surface-border">
      {/* Minimized bar */}
      {minimized ? (
        <div
          className="flex items-center gap-2 sm:gap-3 px-3 sm:px-4 py-2 bg-dark-800/95 backdrop-blur cursor-pointer hover:bg-dark-700/95"
          onClick={() => setMinimized(false)}
        >
          <div className={`w-2 h-2 rounded-full shrink-0 ${
            isCompleted ? 'bg-green-400' : isFailed ? 'bg-red-400'
            : isQueued ? 'bg-amber-400'
            : connected ? 'bg-neon-red animate-pulse' : 'bg-yellow-400'
          }`} />
          <span className="text-[11px] sm:text-xs text-gray-400 font-medium truncate">
            {job.channelName} · {
              cancelling ? 'Cancelando...' : isCompleted ? 'Completado' : isFailed ? 'Error'
              : isQueued ? 'En cola'
              : `${actionLabel(job.action)} · ${pct}%`
            }
          </span>
          <span className="ml-auto text-[10px] text-gray-500 tabular-nums">
            {isQueued ? 'esperando...' : `${pct}%`}
          </span>
          <ChevronUp size={14} className="text-gray-500 shrink-0" />
        </div>
      ) : (
        /* Full bar */
        <div className="bg-dark-800/95 backdrop-blur shadow-2xl shadow-black/30">
          <div className="px-3 sm:px-4 py-2 sm:py-2.5">
            {/* Header row */}
            <div className="flex items-center gap-2 mb-1.5">
              {actionIcon(job.action, 14, `shrink-0 ${
                isCompleted ? 'text-green-400' : isFailed ? 'text-red-400'
                : isQueued ? 'text-amber-400' : 'text-neon-gold'
              }`)}
              <span className="text-xs font-semibold text-white truncate">
                {job.channelName}
              </span>
              {job.videoId && (
                <span className="text-[10px] text-gray-500 shrink-0">
                  #{job.videoId}
                </span>
              )}
              <span className="text-[10px] text-gray-500 truncate">· {actionLabel(job.action)}</span>
              <div className="flex items-center gap-1 ml-auto">
                {isCompleted && <CheckCircle size={14} className="text-green-400 shrink-0" />}
                {isFailed && <AlertCircle size={14} className="text-red-400 shrink-0" />}
                {isQueued && <Hourglass size={12} className="text-amber-400 shrink-0" />}
                {!isQueued && !isCompleted && !isFailed && !cancelling && <Loader2 size={12} className="text-neon-gold animate-spin shrink-0" />}
                {cancelling && <Loader2 size={12} className="text-yellow-400 animate-spin shrink-0" />}
                {!isCompleted && !isFailed && (
                  <button
                    onClick={handleCancel}
                    disabled={cancelling}
                    title={isQueued ? 'Cancelar de la cola' : 'Cancelar generación'}
                    className="p-0.5 text-gray-500 hover:text-red-400 disabled:opacity-40 disabled:cursor-not-allowed"
                  >
                    <Ban size={14} />
                  </button>
                )}
                <button onClick={() => setMinimized(true)}
                  className="p-0.5 text-gray-500 hover:text-white">
                  <ChevronDown size={14} />
                </button>
                <button onClick={() => {
                  if (isCompleted || isFailed) onDismiss()
                  else setDismissed(true)
                }}
                  className="p-0.5 text-gray-500 hover:text-red-400">
                  <X size={14} />
                </button>
              </div>
            </div>

            {/* Progress bar */}
            <div className="relative h-2 bg-dark-900 rounded-full overflow-hidden mb-1">
              <div
                className={`h-full rounded-full transition-all duration-700 ease-out ${
                  isCompleted ? 'bg-green-500' : isFailed ? 'bg-red-500'
                  : isQueued ? 'bg-amber-500/70'
                  : 'bg-gradient-to-r from-neon-red via-orange-500 to-neon-gold'
                }`}
                style={{ width: `${Math.min(pct, 100)}%` }}
              >
                {!isQueued && !isCompleted && !isFailed && (
                  <div className="absolute inset-0 progress-shimmer" />
                )}
              </div>
            </div>

            {/* Phase + status row */}
            <div className="flex items-center justify-between text-[11px]">
              <div className="flex items-center gap-1.5 min-w-0">
                <div className={`w-1.5 h-1.5 rounded-full shrink-0 ${
                  isQueued ? 'bg-amber-400' : connected ? 'bg-green-400' : 'bg-yellow-400'
                }`} />
                {isQueued ? (
                  <>
                    <span className="text-gray-500">cola</span>
                    <span className="text-gray-700">·</span>
                    <span className="text-amber-400 font-medium truncate">En cola — esperando turno</span>
                  </>
                ) : (
                  <>
                    <span className="text-gray-500">
                      {connected ? 'WS' : 'poll'}
                    </span>
                    {progress?.phase && (
                      <>
                        <span className="text-gray-700">·</span>
                        <span className="text-neon-cyan font-medium truncate">{progress.phase}</span>
                      </>
                    )}
                  </>
                )}
              </div>
              <span className={`font-mono font-bold tabular-nums ml-2 ${
                isQueued ? 'text-amber-400' : 'text-neon-red'
              }`}>
                {isQueued ? '—' : `${pct}%`}
              </span>
            </div>

            {/* Message + detail */}
            {progress?.message && (
              <p className="text-[11px] text-gray-300 mt-1 leading-relaxed truncate">
                {progress.message}
              </p>
            )}
            {progress?.detail && (
              <p className="text-[10px] text-slate-500 mt-0.5 truncate">
                {progress.detail}
              </p>
            )}
          </div>
        </div>
      )}
    </div>
  )
}

/** Floating panel containing all active job progress bars. */
export default function GenerationProgressBar() {
  const { activeJobs, clearAll, removeJob } = useGeneration()

  // Show ALL active jobs, including queued ones. Queued jobs (waiting for the
  // global queue consumer) render in an explicit "En cola" state instead of
  // being hidden — the user must see that a generation is waiting its turn.
  // Jobs without a status (optimistically added when the user presses
  // "Generar", before the next API poll) are also kept so dispatch feedback
  // stays instant.
  const visibleJobs = activeJobs

  if (visibleJobs.length === 0) {
    return null
  }

  return (
    <div className="fixed bottom-0 left-0 right-0 z-[9999] transition-all duration-300 animate-slide-up">
      {/* Multi-job indicator */}
      {visibleJobs.length > 1 && (
        <div className="flex items-center gap-1.5 px-3 py-1.5 bg-dark-700/90 backdrop-blur border-t border-surface-border/50">
          <Layers size={12} className="text-neon-gold" />
          <span className="text-[10px] text-gray-400 font-medium">
            {visibleJobs.length} generaciones en curso
          </span>
          <button
            onClick={clearAll}
            className="ml-auto text-[10px] text-gray-500 hover:text-red-400 transition-colors"
          >
            Cerrar todas
          </button>
        </div>
      )}

      {/* Individual job bars */}
      {visibleJobs.map(job => (
        <JobProgressSlot
          key={job.jobId}
          job={job}
          onDismiss={() => removeJob(job.jobId)}
        />
      ))}
    </div>
  )
}
