/** Floating bottom progress bar(s) for video generation — visible from any page.
 *  v2.5: Supports multiple concurrent jobs with stacked bars.
 */

import { useState, useRef, useEffect } from 'react'
import { useGeneration, type ActiveJob } from '../context/GenerationContext'
import { useGenerationProgress, type ProgressData } from '../hooks/useWebSocket'
import { X, ChevronDown, ChevronUp, Loader2, CheckCircle, AlertCircle, Wand2, Layers } from 'lucide-react'

/** Individual progress bar for a single job.
 *  Must be a separate component so useGenerationProgress hook works per jobId. */
function JobProgressSlot({ job, onDismiss }: { job: ActiveJob; onDismiss: () => void }) {
  const [minimized, setMinimized] = useState(false)
  const [dismissed, setDismissed] = useState(false)
  const { progress, connected } = useGenerationProgress(job.jobId)
  const dismissTimerRef = useRef<ReturnType<typeof setTimeout>>()

  const isCompleted = progress?.status === 'completed'
  const isFailed = progress?.status === 'failed'
  const pct = progress?.progress ?? 0

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
            : connected ? 'bg-neon-red animate-pulse' : 'bg-yellow-400'
          }`} />
          <span className="text-[11px] sm:text-xs text-gray-400 font-medium truncate">
            {job.channelName} · {
              isCompleted ? 'Completado' : isFailed ? 'Error'
              : `${pct}% — ${progress?.phase || 'Iniciando...'}`
            }
          </span>
          <span className="ml-auto text-[10px] text-gray-500 tabular-nums">{pct}%</span>
          <ChevronUp size={14} className="text-gray-500 shrink-0" />
        </div>
      ) : (
        /* Full bar */
        <div className="bg-dark-800/95 backdrop-blur shadow-2xl shadow-black/30">
          <div className="px-3 sm:px-4 py-2 sm:py-2.5">
            {/* Header row */}
            <div className="flex items-center gap-2 mb-1.5">
              <Wand2 size={14} className={`shrink-0 ${
                isCompleted ? 'text-green-400' : isFailed ? 'text-red-400' : 'text-neon-gold'
              }`} />
              <span className="text-xs font-semibold text-white truncate">
                {job.channelName}
              </span>
              {job.videoId && (
                <span className="text-[10px] text-gray-500 shrink-0">
                  #{job.videoId}
                </span>
              )}
              <span className="text-[10px] text-gray-500 truncate">· Generar y Subir</span>
              <div className="flex items-center gap-1 ml-auto">
                {isCompleted && <CheckCircle size={14} className="text-green-400 shrink-0" />}
                {isFailed && <AlertCircle size={14} className="text-red-400 shrink-0" />}
                {!isCompleted && !isFailed && <Loader2 size={12} className="text-neon-gold animate-spin shrink-0" />}
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
                  : 'bg-gradient-to-r from-neon-red via-orange-500 to-neon-gold'
                }`}
                style={{ width: `${Math.min(pct, 100)}%` }}
              >
                {!isCompleted && !isFailed && (
                  <div className="absolute inset-0 progress-shimmer" />
                )}
              </div>
            </div>

            {/* Phase + status row */}
            <div className="flex items-center justify-between text-[11px]">
              <div className="flex items-center gap-1.5 min-w-0">
                <div className={`w-1.5 h-1.5 rounded-full shrink-0 ${
                  connected ? 'bg-green-400' : 'bg-yellow-400'
                }`} />
                <span className="text-gray-500">
                  {connected ? 'WS' : 'poll'}
                </span>
                {progress?.phase && (
                  <>
                    <span className="text-gray-700">·</span>
                    <span className="text-neon-cyan font-medium truncate">{progress.phase}</span>
                  </>
                )}
              </div>
              <span className="text-neon-red font-mono font-bold tabular-nums ml-2">{pct}%</span>
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

  if (activeJobs.length === 0) {
    return null
  }

  return (
    <div className="fixed bottom-0 left-0 right-0 z-[9999] transition-all duration-300 animate-slide-up">
      {/* Multi-job indicator */}
      {activeJobs.length > 1 && (
        <div className="flex items-center gap-1.5 px-3 py-1.5 bg-dark-700/90 backdrop-blur border-t border-surface-border/50">
          <Layers size={12} className="text-neon-gold" />
          <span className="text-[10px] text-gray-400 font-medium">
            {activeJobs.length} generaciones activas
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
      {activeJobs.map(job => (
        <JobProgressSlot
          key={job.jobId}
          job={job}
          onDismiss={() => removeJob(job.jobId)}
        />
      ))}
    </div>
  )
}
