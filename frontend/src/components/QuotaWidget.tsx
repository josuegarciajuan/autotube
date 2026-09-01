/** QuotaWidget — YouTube API quota consumption dashboard widget.
 *  Fetches per-PROJECT data from /api/quota/projects every 30s.
 *
 *  La cuota de la Data API v3 es POR PROYECTO GCP (varios canales la
 *  comparten), así que se muestra UNA BARRA POR PROYECTO/CUENTA, cada una
 *  con su propio límite y restante — no un total global contra un único 10k. */
import { useState, useEffect, useCallback } from 'react'
import { motion } from 'framer-motion'
import { BarChart3, Layers, Radio, RefreshCw, Clock, Zap } from 'lucide-react'

interface ProjectChannel {
  slug: string
  units: number
}

interface QuotaProject {
  project_id: string
  account: string
  channels: ProjectChannel[]
  total_units: number
  quota_limit: number
  remaining: number
  exhausted: boolean
  reset_at_utc?: string | null
  remaining_hours?: number | null
}

interface QuotaProjects {
  date: string
  projects: QuotaProject[]
  grand_total_units: number
  by_operation: Record<string, number>
}

// Colores por ÍNDICE de proyecto (no hardcodeado por slug — invariante del repo)
const PROJECT_COLORS = ['#f59e0b', '#06b6d4', '#8b5cf6', '#10b981', '#f43f5e', '#3b82f6']

function projectColor(index: number): string {
  return PROJECT_COLORS[index % PROJECT_COLORS.length]
}

function fmtK(n: number): string {
  return n > 999 ? `${(n / 1000).toFixed(1)}k` : `${n}`
}

export default function QuotaWidget() {
  const [data, setData] = useState<QuotaProjects | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const fetchQuota = useCallback(async () => {
    try {
      const res = await fetch('/api/quota/projects')
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const json = await res.json()
      setData(json)
      setError(null)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchQuota()
    const interval = setInterval(fetchQuota, 30_000)
    return () => clearInterval(interval)
  }, [fetchQuota])

  if (loading && !data) {
    return (
      <div className="flex items-center justify-center py-8 text-gray-500 text-sm">
        <RefreshCw size={14} className="animate-spin mr-2" />
        Cargando cuota...
      </div>
    )
  }

  if (error && !data) {
    return (
      <div className="text-red-400 text-xs py-4 text-center">
        No se pudo cargar: {error}
        <button onClick={fetchQuota} className="ml-2 underline text-blue-400">
          Reintentar
        </button>
      </div>
    )
  }

  if (!data) return null

  const anyExhausted = data.projects.some(p => p.exhausted)

  // Top operations (global — para ver qué consume la cuota)
  const topOps = Object.entries(data.by_operation || {})
    .sort(([, a], [, b]) => b - a)
    .slice(0, 6)

  return (
    <div className="rounded-xl border border-dark-500 bg-dark-800/60 p-4">
      {/* ── Header ── */}
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2">
          <BarChart3 size={16} className="text-gray-400" />
          <span className="text-sm font-semibold text-gray-300">
            Cuota YouTube API
          </span>
          <span className="text-xs text-gray-500">{data.date}</span>
          <span
            className="text-[10px] text-gray-600 border border-dark-600 rounded px-1"
            title="Consumo estimado a partir del registro local de llamadas. No es el contador oficial de Google."
          >
            estimado local
          </span>
        </div>
        <button
          onClick={fetchQuota}
          className="p-1 rounded hover:bg-dark-600 text-gray-500 hover:text-gray-300 transition-colors"
          title="Actualizar"
        >
          <RefreshCw size={14} />
        </button>
      </div>

      {/* ── Per-project bars ── */}
      {data.projects.length === 0 && (
        <div className="text-xs text-gray-500 py-4 text-center">
          Sin datos de consumo hoy.
        </div>
      )}

      {data.projects.map((proj, idx) => {
        const color = projectColor(idx)
        const pct = Math.min((proj.total_units / Math.max(proj.quota_limit, 1)) * 100, 100)
        return (
          <div key={proj.project_id} className="mb-4 last:mb-0">
            {/* Project header */}
            <div className="flex items-center justify-between mb-1">
              <div className="flex items-center gap-2 min-w-0">
                <span className="w-2 h-2 rounded-full flex-shrink-0" style={{ backgroundColor: color }} />
                <span className="text-sm font-medium text-gray-200 truncate">
                  {proj.account || 'Cuenta'}
                </span>
                <span className="text-[10px] text-gray-500 truncate hidden sm:inline">
                  {proj.project_id}
                </span>
              </div>
              <span className={`text-[11px] font-mono tabular-nums flex-shrink-0 ${proj.exhausted ? 'text-red-400' : 'text-gray-400'}`}>
                {proj.total_units.toLocaleString()} / {proj.quota_limit.toLocaleString()} ud
              </span>
            </div>

            {/* Progress bar */}
            <div className="h-2 rounded-full bg-dark-600 overflow-hidden">
              <motion.div
                className="h-full rounded-full overflow-hidden"
                initial={{ width: 0 }}
                animate={{ width: `${pct}%` }}
                transition={{ duration: 0.8, ease: 'easeOut' }}
              >
                <div
                  className="h-full w-full rounded-full"
                  style={{ backgroundColor: proj.exhausted ? '#ef4444' : color }}
                />
              </motion.div>
            </div>
            <div className="flex justify-between text-[10px] text-gray-600 mt-0.5">
              <span>
                {proj.exhausted
                  ? (proj.remaining_hours != null
                      ? `Recarga en ~${proj.remaining_hours.toFixed(1)}h`
                      : 'Agotada')
                  : `${proj.remaining.toLocaleString()} restantes`}
              </span>
              <span>{proj.exhausted ? '100%' : `${pct.toFixed(0)}%`}</span>
            </div>

            {/* Per-channel sub-bars */}
            {proj.channels.length > 0 && (
              <div className="mt-1.5 space-y-1">
                <div className="flex items-center gap-1.5 text-[10px] text-gray-500">
                  <Radio size={10} />
                  <span>Canales</span>
                </div>
                {proj.channels.map(ch => (
                  <div key={ch.slug} className="flex items-center gap-2">
                    <span className="text-[10px] text-gray-400 w-14 truncate">{ch.slug}</span>
                    <div className="flex-1 h-1 rounded-full bg-dark-600 overflow-hidden">
                      <div
                        className="h-full rounded-full transition-all duration-500"
                        style={{
                          width: `${Math.min((ch.units / Math.max(proj.quota_limit, 1)) * 100, 100)}%`,
                          backgroundColor: color,
                          opacity: 0.7,
                        }}
                      />
                    </div>
                    <span className="text-[10px] text-gray-500 w-10 text-right tabular-nums">
                      {fmtK(ch.units)}
                    </span>
                  </div>
                ))}
              </div>
            )}

            {proj.exhausted && (
              <div className="flex items-center gap-1.5 text-[11px] text-red-400 mt-1">
                <Zap size={12} />
                <span>
                  Cuota agotada — recarga{' '}
                  {proj.remaining_hours != null
                    ? `en ~${proj.remaining_hours.toFixed(1)}h`
                    : 'a medianoche (hora del Pacífico)'}
                </span>
              </div>
            )}
          </div>
        )
      })}

      {/* ── Top operations ── */}
      {topOps.length > 0 && (
        <div className="border-t border-dark-500 pt-3 mt-3">
          <div className="flex items-center gap-1.5 text-[11px] text-gray-500 mb-2">
            <Layers size={12} />
            <span>Top operaciones (todas las cuentas)</span>
          </div>
          <div className="space-y-1">
            {topOps.map(([op, units]) => (
              <div key={op} className="flex justify-between text-[11px]">
                <span className="text-gray-400 truncate max-w-[70%]">{op}</span>
                <span className="text-gray-500 tabular-nums">
                  {units.toLocaleString()}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* ── Global exhausted note (informative, not a false alarm) ── */}
      {anyExhausted && (
        <div className="flex items-center gap-1.5 text-[11px] text-red-400 mt-2">
          <Clock size={12} />
          <span>Alguna cuenta agotada — el resto sigue operativo</span>
        </div>
      )}
    </div>
  )
}
