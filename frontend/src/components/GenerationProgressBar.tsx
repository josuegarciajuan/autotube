/** Floating bottom progress bar for video generation — visible from any page. */

import { useState } from 'react'
import { useGeneration } from '../context/GenerationContext'
import { useGenerationProgress, type ProgressData } from '../hooks/useWebSocket'
import { X, ChevronDown, ChevronUp, Loader2, CheckCircle, AlertCircle, Wand2 } from 'lucide-react'

export default function GenerationProgressBar() {
  const { activeJob, clearJob } = useGeneration()
  const [minimized, setMinimized] = useState(false)
  const [dismissed, setDismissed] = useState(false)
  const { progress, connected } = useGenerationProgress(activeJob?.jobId ?? null)

  // Reset dismissed state when a new job starts
  if (activeJob && dismissed) {
    setDismissed(false)
    setMinimized(false)
  }

  if (!activeJob || dismissed) {
    return null
  }

  const isCompleted = progress?.status === 'completed'
  const isFailed = progress?.status === 'failed'
  const pct = progress?.progress ?? 0

  return (
    <div className="fixed bottom-0 left-0 right-0 z-[9999] transition-all duration-300 animate-slide-up">
      {/* Minimized bar */}
      {minimized && (
        <div 
          className="flex items-center gap-3 px-4 py-2 bg-dark-800/95 backdrop-blur border-t border-surface-border cursor-pointer hover:bg-dark-700/95"
          onClick={() => setMinimized(false)}
        >
          <div className={`w-2 h-2 rounded-full ${isCompleted ? 'bg-green-400' : isFailed ? 'bg-red-400' : connected ? 'bg-neon-red animate-pulse' : 'bg-yellow-400'}`} />
          <span className="text-xs text-gray-400 font-medium">
            {activeJob.channelName} · {
              isCompleted ? 'Completado' : isFailed ? 'Error' : `${pct}% — ${progress?.phase || 'Iniciando...'}`
            }
          </span>
          <span className="ml-auto text-[10px] text-gray-600">
            {isCompleted ? 'Listo ✓' : isFailed ? 'Falló ✗' : `${pct}%`}
          </span>
          <ChevronUp size={14} className="text-gray-500" />
        </div>
      )}

      {/* Full bar */}
      {!minimized && (
        <div className="bg-dark-800/95 backdrop-blur border-t border-surface-border shadow-2xl shadow-black/50">
          <div className="max-w-6xl mx-auto px-4 py-3">
            {/* Header row */}
            <div className="flex items-center gap-3 mb-2">
              <Wand2 size={16} className={isCompleted ? 'text-green-400' : isFailed ? 'text-red-400' : 'text-neon-gold'} />
              <span className="text-sm font-medium text-white truncate">
                {activeJob.channelName} · Generar y Subir
              </span>
              <div className="flex items-center gap-1.5 ml-auto">
                {isCompleted && <CheckCircle size={16} className="text-green-400" />}
                {isFailed && <AlertCircle size={16} className="text-red-400" />}
                {!isCompleted && !isFailed && <Loader2 size={14} className="text-neon-gold animate-spin" />}
                <button onClick={() => setMinimized(true)} className="p-0.5 text-gray-500 hover:text-white">
                  <ChevronDown size={16} />
                </button>
                <button onClick={() => { clearJob(); setDismissed(true) }} className="p-0.5 text-gray-500 hover:text-red-400">
                  <X size={16} />
                </button>
              </div>
            </div>

            {/* Progress bar */}
            <div className="relative h-2.5 bg-dark-900 rounded-full overflow-hidden mb-1.5">
              <div
                className={`h-full rounded-full transition-all duration-700 ease-out ${
                  isCompleted ? 'bg-green-500' : isFailed ? 'bg-red-500' : 'bg-gradient-to-r from-neon-red via-orange-500 to-neon-gold'
                }`}
                style={{ width: `${Math.min(pct, 100)}%` }}
              >
                {!isCompleted && !isFailed && (
                  <div className="absolute inset-0 progress-shimmer" />
                )}
              </div>
            </div>

            {/* Phase + message row */}
            <div className="flex items-center justify-between text-xs">
              <div className="flex items-center gap-2">
                <div className={`w-2 h-2 rounded-full ${connected ? 'bg-green-400' : 'bg-yellow-400'}`} />
                <span className="text-gray-400">
                  {connected ? 'Conectado' : 'Reconectando...'}
                </span>
                {progress?.phase && (
                  <>
                    <span className="text-gray-600">·</span>
                    <span className="text-neon-cyan font-medium">{progress.phase}</span>
                  </>
                )}
              </div>
              <span className="text-neon-red font-mono font-bold tabular-nums">{pct}%</span>
            </div>

            {progress?.message && (
              <p className="text-xs text-gray-300 mt-1.5 font-medium leading-relaxed">{progress.message}</p>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
