/** ExecutionTimeline — Shows past and running generation jobs.
 *  Pure history view: completed, failed, cancelled, running.
 *  Future slots are shown in UpcomingExecutions instead.
 */

import { useState, useEffect, useCallback } from 'react'
import { api, formatDateTime } from '../lib/api'
import { Clock, CheckCircle, AlertCircle, Loader2, Filter, RefreshCw, Play, Youtube, X, XCircle } from 'lucide-react'

type FilterStatus = 'all' | 'running' | 'completed' | 'failed' | 'cancelled'

interface Execution {
  id: string
  date: string
  channel_name: string
  action: string
  status: string
  progress?: number
  phase?: string
  video_id?: number
  error_msg?: string
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
      const [jobs, chs] = await Promise.all([
        api.getJobs(undefined, filterChannel || undefined, 50),
        api.getChannels(true),
      ])
      setChannels(chs)

      const execs: Execution[] = []

      for (const j of jobs) {
        const chName = j.channel_name || chs.find((c: any) => c.id === j.channel_id)?.name || `Canal #${j.channel_id}`

        execs.push({
          id: `job_${j.id}`,
          date: j.finished_at || j.started_at || j.created_at,
          channel_name: chName,
          action: j.action || 'generate_and_upload',
          status: j.status,
          progress: j.progress || 0,
          phase: j.phase || '',
          video_id: j.video_id,
          error_msg: j.error_msg,
        })
      }

      // Sort by date descending (newest first)
      execs.sort((a, b) => new Date(b.date).getTime() - new Date(a.date).getTime())
      setExecutions(execs)
    } catch (e) { console.error(e) }
    setLoading(false)
  }, [filterChannel])

  useEffect(() => {
    loadData()
    const interval = setInterval(loadData, 15000)
    return () => clearInterval(interval)
  }, [loadData])

  const filtered = executions.filter(e => {
    if (filterStatus === 'running') return e.status === 'running'
    if (filterStatus === 'completed') return e.status === 'completed'
    if (filterStatus === 'failed') return e.status === 'failed'
    if (filterStatus === 'cancelled') return e.status === 'cancelled'
    return true
  })

  const counts = {
    running: executions.filter(e => e.status === 'running').length,
    completed: executions.filter(e => e.status === 'completed').length,
    failed: executions.filter(e => e.status === 'failed').length,
    cancelled: executions.filter(e => e.status === 'cancelled').length,
  }

  const filterOptions: [FilterStatus, string, number][] = [
    ['all', `Todas (${executions.length})`, executions.length],
    ['running', `Ejecutando (${counts.running})`, counts.running],
    ['completed', `Completadas (${counts.completed})`, counts.completed],
    ['failed', `Fallidas (${counts.failed})`, counts.failed],
    ['cancelled', `Canceladas (${counts.cancelled})`, counts.cancelled],
  ]

  return (
    <section className="glass rounded-xl p-5">
      {/* Header */}
      <div className="flex items-center justify-between mb-4">
        <h3 className="font-display text-base font-semibold text-white flex items-center gap-2">
          <Clock size={16} className="text-neon-cyan" /> Historial de Ejecuciones
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
          <div className="flex flex-wrap gap-1">
            {filterOptions.map(([val, label, count]) => (
              <button key={val}
                onClick={() => setFilterStatus(val)}
                className={`px-2.5 py-1 rounded text-xs font-medium transition-colors ${
                  filterStatus === val ? 'bg-neon-cyan/20 text-neon-cyan' : 'text-gray-400 hover:text-white hover:bg-dark-600'
                }`}>
                {label}
              </button>
            ))}
          </div>
          <span className="text-gray-700 hidden sm:inline">|</span>
          <select value={filterChannel} onChange={e => { setFilterChannel(Number(e.target.value)); setLoading(true) }}
            className="px-2 py-1 bg-dark-600 border border-surface-border rounded text-xs text-gray-300 focus:outline-none focus:border-neon-cyan">
            <option value={0}>Todos los canales</option>
            {channels.filter((c: any) => c.slug !== 'test').map((ch: any) => (
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
  const isRunning = e.status === 'running'

  function statusIcon() {
    switch (e.status) {
      case 'running': return <Loader2 size={14} className="text-neon-gold animate-spin" />
      case 'completed': return <CheckCircle size={14} className="text-green-400" />
      case 'failed': return <AlertCircle size={14} className="text-red-400" />
      case 'cancelled': return <XCircle size={14} className="text-gray-500" />
      default: return <Clock size={14} className="text-gray-500" />
    }
  }

  function statusText() {
    switch (e.status) {
      case 'running': return 'Ejecutando'
      case 'completed': return 'Completado'
      case 'failed': return `Fallido${e.error_msg ? ': ' + e.error_msg.slice(0, 80) : ''}`
      case 'cancelled': return 'Cancelado'
      default: return e.status
    }
  }

  function statusColor() {
    switch (e.status) {
      case 'running': return 'text-neon-gold'
      case 'completed': return 'text-green-400/80'
      case 'failed': return 'text-red-400/80'
      case 'cancelled': return 'text-gray-500'
      default: return 'text-gray-400'
    }
  }

  return (
    <div className={`flex items-center gap-2 sm:gap-3 px-3 py-2 text-xs border-b border-surface-border/30 last:border-0 hover:bg-dark-700/30 transition-colors rounded flex-wrap ${
      isRunning ? 'bg-neon-gold/5' : e.status === 'failed' ? 'bg-red-900/10' : ''
    }`}>
      {/* Date/Time */}
      <span className={`font-mono shrink-0 ${isRunning ? 'text-neon-gold' : 'text-gray-400'}`}>
        {formatDateTime(e.date)}
      </span>

      {/* Status indicator */}
      <div className="shrink-0">{statusIcon()}</div>

      {/* Channel */}
      <span className="font-medium text-gray-300 truncate shrink-0 max-w-[100px] sm:max-w-[150px]">
        {e.channel_name}
      </span>

      {/* Action */}
      <span className="text-gray-600 shrink-0">
        {e.action === 'generate_and_upload' || e.action === 'generate_clip_short' || e.action === 'generate_native_short' ? (
          <span className="flex items-center gap-1"><Youtube size={10} /> Subir</span>
        ) : (
          <span className="flex items-center gap-1"><Play size={10} /> {e.action}</span>
        )}
      </span>

      {/* Status text + progress */}
      <span className={`flex-1 min-w-0 ${statusColor()}`}>
        {isRunning ? (
          <div className="flex items-center gap-2">
            <div className="flex-1 h-1.5 bg-dark-700 rounded-full overflow-hidden max-w-[100px]">
              <div className="h-full bg-neon-gold rounded-full transition-all duration-500" style={{ width: `${e.progress || 0}%` }} />
            </div>
            <span className="text-neon-gold font-mono tabular-nums">{e.progress || 0}%</span>
            {e.phase && <span className="text-gray-500">{e.phase}</span>}
          </div>
        ) : (
          <span className="truncate" title={e.error_msg}>{statusText()}</span>
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
