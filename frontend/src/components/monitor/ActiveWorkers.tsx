import { useState } from 'react'
import { Film, Smartphone, Activity, Play, Terminal } from 'lucide-react'
import { useActiveWorkers } from '../../hooks/useQueries'
import { CHANNEL_PILL } from '../../lib/channelConfig'
import LiveLogs from './LiveLogs'

interface Worker {
  type: 'long' | 'short'
  job_id: number
  video_id?: number
  short_id?: number
  channel?: string
  channel_id?: number
  title?: string
  status: string
  progress: number
  phase: string
  pipeline_phase?: string
  action?: string
  started_at?: string
  elapsed_seconds: number
  worker_pid?: number
  worker_ram_mb?: number
  retry_count?: number
  error_msg?: string
  short_type?: string
}

export default function ActiveWorkers() {
  const { data: rawData } = useActiveWorkers()
  const [selectedJobId, setSelectedJobId] = useState<number | null>(null)
  const workers: Worker[] = rawData?.ok ? (rawData.workers || []) : []

  function fmtElapsed(s: number): string {
    const m = Math.floor(s / 60)
    const h = Math.floor(m / 60)
    if (h > 0) return `${h}h ${m % 60}m`
    return `${m}m ${s % 60}s`
  }

  function phaseColor(phase: string) {
    const map: Record<string, string> = {
      scrape: 'bg-cyan-500', script: 'bg-blue-500', tts: 'bg-purple-500',
      media: 'bg-pink-500', images: 'bg-pink-500', video: 'bg-amber-500',
      metadata: 'bg-teal-500', upload: 'bg-emerald-500', error: 'bg-red-500',
      extracting: 'bg-indigo-500', rendering: 'bg-amber-500', uploading: 'bg-emerald-500',
    }
    return map[phase] || 'bg-gray-500'
  }

  if (workers.length === 0) {
    return (
      <div className="glass rounded-xl p-5 border border-surface-border">
        <h3 className="text-sm font-semibold text-gray-300 flex items-center gap-2 mb-4">
          <Activity size={14} className="text-gray-500" />
          Workers Activos
        </h3>
        <div className="text-center py-6">
          <div className="text-4xl mb-2">😴</div>
          <p className="text-gray-500 text-sm">Sin workers activos</p>
          <p className="text-gray-600 text-xs mt-1">No hay generaciones en curso</p>
        </div>
      </div>
    )
  }

  return (
    <div className="glass rounded-xl p-5 border border-surface-border">
      <h3 className="text-sm font-semibold text-gray-300 flex items-center gap-2 mb-4">
        <Activity size={14} className={workers.length > 0 ? 'text-emerald-400 animate-pulse' : 'text-gray-500'} />
        Workers Activos ({workers.length})
      </h3>
      <div className="space-y-3">
        {workers.map((w, i) => (
          <div key={`${w.type}-${w.job_id || w.short_id}-${i}`}
               className="bg-dark-700/50 rounded-lg p-3 border border-surface-border/30">
            {/* Header row */}
            <div className="flex items-center gap-2 mb-2">
              {w.type === 'long' ? <Film size={13} className="text-amber-400" /> : <Smartphone size={13} className="text-emerald-400" />}
              <span className="text-xs font-medium text-gray-300">
                {w.type === 'long' ? `Video #${w.video_id}` : `Short #${w.short_id}`}
              </span>
              {w.channel && (
                <span className={`text-[10px] px-1.5 py-0.5 rounded-full font-medium ${CHANNEL_PILL[w.channel] || 'bg-gray-500/20 text-gray-400'}`}>
                  {w.channel}
                </span>
              )}
              <span className="text-[10px] text-gray-500 ml-auto">{w.action}</span>
            </div>

            {/* Progress bar */}
            <div className="relative h-2 bg-dark-600 rounded-full overflow-hidden mb-1.5">
              <div
                className={`absolute inset-y-0 left-0 rounded-full transition-all duration-1000 ${phaseColor(w.phase)}`}
                style={{ width: `${Math.max(2, w.progress)}%` }}
              />
              <div className="absolute inset-0 progress-shimmer opacity-30" />
            </div>

            {/* Info row */}
            <div className="flex items-center gap-3 text-[10px] text-gray-500">
              <span className="flex items-center gap-1">
                <Play size={10} />
                {w.phase}
              </span>
              <span>{w.progress}%</span>
              <span>{fmtElapsed(w.elapsed_seconds)}</span>
              {w.worker_pid && (
                <span>PID {w.worker_pid}</span>
              )}
              {w.worker_ram_mb != null && (
                <span>{w.worker_ram_mb} MB</span>
              )}
              {w.retry_count != null && w.retry_count > 0 && (
                <span className="text-amber-400">Retry #{w.retry_count}</span>
              )}
              {w.job_id && (
                <button
                  onClick={() => setSelectedJobId(selectedJobId === w.job_id ? null : w.job_id!)}
                  className="ml-auto flex items-center gap-1 px-1.5 py-0.5 text-neon-cyan hover:bg-neon-cyan/10 rounded transition-colors"
                >
                  <Terminal size={10} />
                  {selectedJobId === w.job_id ? 'Ocultar logs' : 'Logs'}
                </button>
              )}
            </div>

            {/* Error message if any */}
            {w.error_msg && (
              <div className="mt-1.5 text-[10px] text-red-400 bg-red-500/10 rounded px-2 py-1">
                {w.error_msg}
              </div>
            )}
          </div>
        ))}
      </div>

      {/* Inline LiveLogs panel */}
      {selectedJobId && (
        <div className="mt-4">
          <LiveLogs jobId={selectedJobId} />
        </div>
      )}
    </div>
  )
}
