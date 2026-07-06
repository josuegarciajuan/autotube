/** Unified execution timeline — past, in-progress, and future pipeline executions.

Shows:
- Past: completed/failed jobs
- In progress: running jobs with % progress bar
- Future: scheduled upcoming runs (next 7 days)

Supports filtering by status and channel.
*/

import { useState, useEffect, useCallback } from 'react'
import { api, formatDateTime } from '../lib/api'
import { Clock, CheckCircle, AlertCircle, Loader2, Calendar, Filter, RefreshCw, Play, Youtube, X, ChevronLeft, ChevronRight } from 'lucide-react'

type FilterStatus = 'all' | 'past' | 'running' | 'future'

interface Execution {
  id: string            // unique key: "job_123" or "sched_456_run_2024-01-01"
  type: 'past' | 'running' | 'future'
  date: string          // ISO datetime
  channel_name: string
  action: string
  status: string         // completed / failed / running / scheduled
  progress?: number      // 0-100 for running
  phase?: string         // current phase for running
  video_id?: number
  video_title?: string
  error_msg?: string
  schedule_id?: number
}

export default function ExecutionTimeline() {
  const [executions, setExecutions] = useState<Execution[]>([])
  const [loading, setLoading] = useState(true)
  const [filterStatus, setFilterStatus] = useState<FilterStatus>('all')
  const [filterChannel, setFilterChannel] = useState<number>(0)
  const [channels, setChannels] = useState<any[]>([])
  const [showFilters, setShowFilters] = useState(false)

  const loadData = useCallback(async () => {
    try {
      const [jobs, schedules, chs] = await Promise.all([
        api.getJobs(undefined, filterChannel || undefined, 50),
        api.getSchedules(filterChannel || undefined, false),
        api.getChannels(true),
      ])
      setChannels(chs)

      const execs: Execution[] = []

      // ── Past + Running Jobs ──────────────────────────────
      for (const j of jobs) {
        const isRunning = j.status === 'running'
        const isCompleted = j.status === 'completed'
        const isFailed = j.status === 'failed'

        if (isRunning) {
          execs.push({
            id: `job_${j.id}`,
            type: 'running',
            date: j.started_at || j.created_at,
            channel_name: j.channel_name || chs.find((c: any) => c.id === j.channel_id)?.name || `Canal #${j.channel_id}`,
            action: j.action || 'generate_and_upload',
            status: 'running',
            progress: j.progress || 0,
            phase: j.phase || 'iniciando',
            video_id: j.video_id,
          })
        } else if (isCompleted || isFailed) {
          execs.push({
            id: `job_${j.id}`,
            type: 'past',
            date: j.finished_at || j.started_at || j.created_at,
            channel_name: j.channel_name || chs.find((c: any) => c.id === j.channel_id)?.name || `Canal #${j.channel_id}`,
            action: j.action || 'generate_and_upload',
            status: isCompleted ? 'completed' : 'failed',
            video_id: j.video_id,
            error_msg: j.error_msg,
          })
        }
      }

      // ── Future Scheduled Runs ────────────────────────────
      const now = new Date()
      const sevenDaysFromNow = new Date(now.getTime() + 7 * 24 * 60 * 60 * 1000)

      for (const s of schedules) {
        if (!s.active || !s.next_run_at) continue
        
        const nextRun = new Date(s.next_run_at)
        if (nextRun > sevenDaysFromNow) {
          // Only show 1 upcoming run beyond 7 days
          execs.push({
            id: `sched_${s.id}_run_0`,
            type: 'future',
            date: s.next_run_at,
            channel_name: s.channel_name || chs.find((c: any) => c.id === s.channel_id)?.name || '',
            action: s.action,
            status: 'scheduled',
            schedule_id: s.id,
          })
          continue
        }

        // For recurring schedules, generate next 7 days of runs
        if (s.schedule_type === 'recurring') {
          let runTime = nextRun
          let idx = 0
          while (runTime <= sevenDaysFromNow && idx < 50) {
            execs.push({
              id: `sched_${s.id}_run_${idx}`,
              type: 'future',
              date: runTime.toISOString(),
              channel_name: s.channel_name || chs.find((c: any) => c.id === s.channel_id)?.name || '',
              action: s.action,
              status: 'scheduled',
              schedule_id: s.id,
            })
            runTime = new Date(runTime.getTime() + (s.interval_h || 24) * 60 * 60 * 1000)
            idx++
          }
        } else {
          execs.push({
            id: `sched_${s.id}_run_0`,
            type: 'future',
            date: s.next_run_at,
            channel_name: s.channel_name || chs.find((c: any) => c.id === s.channel_id)?.name || '',
            action: s.action,
            status: 'scheduled',
            schedule_id: s.id,
          })
        }
      }

      // Sort by date (descending for past, ascending for future)
      const past = execs.filter(e => e.type === 'past').sort((a, b) => new Date(b.date).getTime() - new Date(a.date).getTime())
      const running = execs.filter(e => e.type === 'running').sort((a, b) => new Date(b.date).getTime() - new Date(a.date).getTime())
      const future = execs.filter(e => e.type === 'future').sort((a, b) => new Date(a.date).getTime() - new Date(b.date).getTime())

      setExecutions([...running, ...past, ...future])
    } catch (e) { console.error(e) }
    setLoading(false)
  }, [filterChannel])

  useEffect(() => {
    loadData()
    const interval = setInterval(loadData, 15000) // Refresh every 15s
    return () => clearInterval(interval)
  }, [loadData])

  // ── Filtering ────────────────────────────────────────────
  const filtered = executions.filter(e => {
    if (filterStatus === 'past' && e.type !== 'past') return false
    if (filterStatus === 'running' && e.type !== 'running') return false
    if (filterStatus === 'future' && e.type !== 'future') return false
    return true
  })

  const pastCount = executions.filter(e => e.type === 'past').length
  const runningCount = executions.filter(e => e.type === 'running').length
  const futureCount = executions.filter(e => e.type === 'future').length

  return (
    <section className="glass rounded-xl p-5">
      {/* Header */}
      <div className="flex items-center justify-between mb-4">
        <h3 className="font-display text-base font-semibold text-white flex items-center gap-2">
          <Clock size={16} className="text-neon-cyan" /> Ejecuciones (próx. 7 días)
        </h3>
        <div className="flex items-center gap-2">
          <button onClick={() => setShowFilters(!showFilters)}
            className={`flex items-center gap-1 px-2.5 py-1.5 rounded-lg text-xs font-medium transition-colors ${
              showFilters ? 'bg-neon-cyan/10 text-neon-cyan border border-neon-cyan/30' : 'text-gray-400 hover:text-white border border-transparent hover:border-gray-600'
            }`}>
            <Filter size={12} /> Filtros
          </button>
          <button onClick={loadData} className="p-1.5 text-gray-400 hover:text-white rounded-lg hover:bg-dark-600">
            <RefreshCw size={14} className={loading ? 'animate-spin' : ''} />
          </button>
        </div>
      </div>

      {/* Filter bar */}
      {showFilters && (
        <div className="flex flex-col sm:flex-row flex-wrap items-start sm:items-center gap-2 mb-4 p-3 bg-dark-700/50 rounded-lg">
          <div className="flex gap-1">
            {([
              ['all', `Todas (${executions.length})`],
              ['running', `En curso (${runningCount})`],
              ['past', `Pasadas (${pastCount})`],
              ['future', `Futuras (${futureCount})`],
            ] as [FilterStatus, string][]).map(([val, label]) => (
              <button key={val}
                onClick={() => setFilterStatus(val)}
                className={`px-2.5 py-1 rounded text-xs font-medium transition-colors ${
                  filterStatus === val ? 'bg-neon-cyan/20 text-neon-cyan' : 'text-gray-400 hover:text-white hover:bg-dark-600'
                }`}>
                {label}
              </button>
            ))}
          </div>
          <span className="text-gray-700">|</span>
          <select value={filterChannel} onChange={e => { setFilterChannel(Number(e.target.value)); setLoading(true) }}
            className="px-2 py-1 bg-dark-600 border border-surface-border rounded text-xs text-gray-300 focus:outline-none focus:border-neon-cyan">
            <option value={0}>Todos los canales</option>
            {channels.map((ch: any) => (
              <option key={ch.id} value={ch.id}>{ch.name}</option>
            ))}
          </select>
          {(filterStatus !== 'all' || filterChannel !== 0) && (
            <button onClick={() => { setFilterStatus('all'); setFilterChannel(0); setLoading(true) }}
              className="flex items-center gap-1 px-2 py-1 text-xs text-red-400 hover:text-red-300">
              <X size={12} /> Limpiar
            </button>
          )}
        </div>
      )}

      {/* Timeline */}
      {loading && executions.length === 0 ? (
        <div className="flex items-center justify-center py-12">
          <Loader2 size={20} className="animate-spin text-gray-600" />
        </div>
      ) : filtered.length === 0 ? (
        <p className="text-sm text-gray-500 py-8 text-center">
          No hay ejecuciones {filterStatus !== 'all' ? 'con este filtro' : 'registradas'}
        </p>
      ) : (
        <div className="space-y-1 max-h-[600px] overflow-y-auto">
          {filtered.map((e) => (
            <ExecutionRow key={e.id} execution={e} />
          ))}
        </div>
      )}
    </section>
  )
}

function ExecutionRow({ execution: e }: { execution: Execution }) {
  const isRunning = e.type === 'running'
  const isPast = e.type === 'past'
  const isFuture = e.type === 'future'

  return (
    <div className={`flex items-center gap-2 sm:gap-3 px-3 py-2 text-xs border-b border-surface-border/30 last:border-0 hover:bg-dark-700/30 transition-colors rounded flex-wrap ${
      isRunning ? 'bg-neon-gold/5' : isPast && e.status === 'failed' ? 'bg-red-900/10' : ''
    }`}>
      {/* Date/Time */}
      <span className={`font-mono shrink-0 ${
        isFuture ? 'text-gray-500' : isRunning ? 'text-neon-gold' : 'text-gray-400'
      }`}>
        {formatDateTime(e.date)}
      </span>

      {/* Status indicator */}
      <div className="shrink-0">
        {isRunning && <Loader2 size={14} className="text-neon-gold animate-spin" />}
        {isPast && e.status === 'completed' && <CheckCircle size={14} className="text-green-400" />}
        {isPast && e.status === 'failed' && <AlertCircle size={14} className="text-red-400" />}
        {isFuture && <Calendar size={14} className="text-gray-600" />}
      </div>

      {/* Channel */}
      <span className="font-medium text-gray-300 truncate shrink-0 max-w-[100px] sm:max-w-[150px]">{e.channel_name}</span>

      {/* Action */}
      <span className="text-gray-600 shrink-0">
        {e.action === 'generate_and_upload' ? (
          <span className="flex items-center gap-1"><Youtube size={10} /> Subir</span>
        ) : (
          <span className="flex items-center gap-1"><Play size={10} /> Generar</span>
        )}
      </span>

      {/* Status text + progress */}
      <span className="flex-1 min-w-0">
        {isRunning && (
          <div className="flex items-center gap-2">
            <div className="flex-1 h-1.5 bg-dark-700 rounded-full overflow-hidden max-w-[100px]">
              <div className="h-full bg-neon-gold rounded-full transition-all duration-500" style={{ width: `${e.progress || 0}%` }} />
            </div>
            <span className="text-neon-gold font-mono tabular-nums">{e.progress || 0}%</span>
            {e.phase && <span className="text-gray-500">{e.phase}</span>}
          </div>
        )}
        {isPast && e.status === 'completed' && (
          <span className="text-green-400/80">Completado</span>
        )}
        {isPast && e.status === 'failed' && (
          <span className="text-red-400/80 truncate" title={e.error_msg}>
            Fallido{e.error_msg ? `: ${e.error_msg.slice(0, 60)}` : ''}
          </span>
        )}
        {isFuture && (
          <span className="text-gray-600">Programado</span>
        )}
      </span>

      {/* Link to video */}
      {e.video_id && (
        <a href={`/videos/${e.video_id}/edit`} className="text-neon-cyan hover:underline shrink-0 text-[11px]">
          Video #{e.video_id}
        </a>
      )}
    </div>
  )
}
