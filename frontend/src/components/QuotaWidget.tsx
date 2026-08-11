/** QuotaWidget — YouTube API quota consumption dashboard widget.
 *  Fetches passive tracking data from /api/quota/daily every 30s.
 *  Shows a progress bar + per-channel + per-operation breakdown. */
import { useState, useEffect, useCallback } from 'react'
import { motion } from 'framer-motion'
import { BarChart3, Layers, Radio, RefreshCw, Clock, Zap } from 'lucide-react'

interface QuotaDaily {
  date: string
  total_units: number
  quota_limit: number
  remaining: number
  exhausted_estimated_at: string | null
  by_channel: Record<string, number>
  by_operation: Record<string, number>
}

const CHANNEL_COLORS: Record<string, string> = {
  canal2: '#f59e0b',
  canal3: '#8b5cf6',
  canal4: '#06b6d4',
  canal5: '#10b981',
}

function getChannelColor(slug: string): string {
  return CHANNEL_COLORS[slug] || '#6b7280'
}

export default function QuotaWidget() {
  const [data, setData] = useState<QuotaDaily | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const fetchQuota = useCallback(async () => {
    try {
      const res = await fetch('/api/quota/daily')
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

  const pct = Math.min((data.total_units / data.quota_limit) * 100, 100)
  const isExhausted = data.remaining <= 0
  const isWarning = pct > 80 && !isExhausted

  // Top operations
  const topOps = Object.entries(data.by_operation)
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
        </div>
        <button
          onClick={fetchQuota}
          className="p-1 rounded hover:bg-dark-600 text-gray-500 hover:text-gray-300 transition-colors"
          title="Actualizar"
        >
          <RefreshCw size={14} />
        </button>
      </div>

      {/* ── Progress bar ── */}
      <div className="mb-3">
        <div className="flex justify-between text-xs text-gray-500 mb-1">
          <span>{data.total_units.toLocaleString()} usados</span>
          <span>{data.remaining.toLocaleString()} restantes</span>
        </div>
        <div className="h-2.5 rounded-full bg-dark-600 overflow-hidden">
          <motion.div
            className={`h-full rounded-full ${
              isExhausted
                ? 'bg-red-500'
                : isWarning
                ? 'bg-yellow-500'
                : 'bg-green-500'
            }`}
            initial={{ width: 0 }}
            animate={{ width: `${pct}%` }}
            transition={{ duration: 0.8, ease: 'easeOut' }}
          />
        </div>
        <div className="flex justify-between text-[10px] text-gray-600 mt-0.5">
          <span>0</span>
          <span>5k</span>
          <span>10k</span>
        </div>
      </div>

      {/* ── Exhaustion estimate ── */}
      {data.exhausted_estimated_at && !isExhausted && (
        <div className="flex items-center gap-1.5 text-[11px] text-gray-400 mb-3">
          <Clock size={12} />
          <span>
            Agotamiento estimado:{' '}
            {new Date(data.exhausted_estimated_at).toLocaleTimeString('es-ES', {
              hour: '2-digit',
              minute: '2-digit',
            })}
          </span>
        </div>
      )}

      {isExhausted && (
        <div className="flex items-center gap-1.5 text-[11px] text-red-400 mb-3">
          <Zap size={12} />
          <span>Cuota agotada — recarga a medianoche (hora del Pacífico)</span>
        </div>
      )}

      {/* ── Per-channel bars ── */}
      {Object.keys(data.by_channel).length > 0 && (
        <div className="mb-3">
          <div className="flex items-center gap-1.5 text-[11px] text-gray-500 mb-2">
            <Radio size={12} />
            <span>Por canal</span>
          </div>
          {Object.entries(data.by_channel)
            .sort(([, a], [, b]) => b - a)
            .map(([channel, units]) => (
              <div key={channel} className="flex items-center gap-2 mb-1">
                <div
                  className="w-2 h-2 rounded-full flex-shrink-0"
                  style={{ backgroundColor: getChannelColor(channel) }}
                />
                <span className="text-xs text-gray-400 w-14 truncate">
                  {channel}
                </span>
                <div className="flex-1 h-1.5 rounded-full bg-dark-600">
                  <div
                    className="h-full rounded-full transition-all duration-500"
                    style={{
                      width: `${Math.min((units / data.quota_limit) * 100, 100)}%`,
                      backgroundColor: getChannelColor(channel),
                    }}
                  />
                </div>
                <span className="text-[10px] text-gray-500 w-10 text-right tabular-nums">
                  {units > 999 ? `${(units / 1000).toFixed(1)}k` : units}
                </span>
              </div>
            ))}
        </div>
      )}

      {/* ── Top operations ── */}
      {topOps.length > 0 && (
        <div>
          <div className="flex items-center gap-1.5 text-[11px] text-gray-500 mb-2">
            <Layers size={12} />
            <span>Top operaciones</span>
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
    </div>
  )
}
