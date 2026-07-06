/** Floating progress/result bar for system stabilization.
 *  Shows a loading spinner during stabilization, then results + auto-dismiss.
 *  Uses the same visual pattern as GenerationProgressBar.
 */

import { useState } from 'react'
import { X, Wrench, CheckCircle, AlertCircle, Loader2, Zap } from 'lucide-react'

export interface StabilizeResult {
  success: boolean
  steps: string[]
  total_killed: number
  total_freed: string
  disk_free: string
  message: string
}

interface Props {
  result: StabilizeResult | null
  loading: boolean
  error: string | null
  onDismiss: () => void
}

export default function StabilizeProgress({ result, loading, error, onDismiss }: Props) {
  const [dismissed, setDismissed] = useState(false)

  if (dismissed || (!loading && !result && !error)) {
    return null
  }

  return (
    <div className="fixed bottom-0 left-0 right-0 z-[9998] transition-all duration-300 animate-slide-up">
      <div className="bg-dark-800/95 backdrop-blur shadow-2xl shadow-black/30 border-t border-surface-border">
        <div className="px-3 sm:px-4 py-2 sm:py-2.5">
          {/* Header */}
          <div className="flex items-center gap-2 mb-1.5">
            {loading ? (
              <Loader2 size={14} className="text-neon-gold animate-spin shrink-0" />
            ) : result ? (
              <CheckCircle size={14} className="text-green-400 shrink-0" />
            ) : (
              <AlertCircle size={14} className="text-red-400 shrink-0" />
            )}
            <span className="text-xs font-semibold text-white">
              {loading ? 'Estabilizando herramienta...' : result ? 'Estabilización completada' : 'Error en estabilización'}
            </span>
            <button
              onClick={() => setDismissed(true)}
              className="ml-auto p-0.5 text-gray-500 hover:text-red-400"
            >
              <X size={14} />
            </button>
          </div>

          {/* Progress bar - undetermined animation when loading */}
          {loading && (
            <div className="relative h-2 bg-dark-900 rounded-full overflow-hidden mb-1.5">
              <div className="absolute inset-0 progress-shimmer rounded-full" />
            </div>
          )}

          {/* Results */}
          {result && (
            <div className="space-y-1">
              {result.total_killed > 0 && (
                <div className="flex items-center gap-1.5 text-[11px]">
                  <Zap size={12} className="text-neon-gold shrink-0" />
                  <span className="text-gray-300">{result.total_killed} procesos eliminados</span>
                </div>
              )}
              {result.total_freed !== '0 B' && (
                <div className="flex items-center gap-1.5 text-[11px]">
                  <Wrench size={12} className="text-neon-cyan shrink-0" />
                  <span className="text-gray-300">{result.total_freed} liberados en disco</span>
                  <span className="text-[10px] text-gray-500">· Libre: {result.disk_free}</span>
                </div>
              )}
              {/* Show first 3 steps */}
              {result.steps.slice(0, 3).map((step, i) => (
                <div key={i} className="flex items-center gap-1.5 text-[10px]">
                  <CheckCircle size={10} className="text-green-400 shrink-0" />
                  <span className="text-gray-400">{step}</span>
                </div>
              ))}
              {result.steps.length > 3 && (
                <p className="text-[10px] text-gray-500 pl-5">
                  +{result.steps.length - 3} acciones más
                </p>
              )}
              <p className="text-[10px] text-neon-gold mt-1">La API se reiniciará automáticamente...</p>
            </div>
          )}

          {/* Error */}
          {error && (
            <p className="text-[11px] text-red-400 mt-1">{error}</p>
          )}
        </div>
      </div>
    </div>
  )
}
