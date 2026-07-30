import { useState, useEffect, useRef } from 'react'
import { Activity, MemoryStick, Bell } from 'lucide-react'
import { api } from '../../lib/api'

interface StatusBarData {
  workers: number
  long_running: number
  shorts_running: number
  ram_available_mb: number | null
  critical_alerts: number
}

export default function StatusBar() {
  const [data, setData] = useState<StatusBarData>({
    workers: 0, long_running: 0, shorts_running: 0,
    ram_available_mb: null, critical_alerts: 0,
  })
  const timerRef = useRef<ReturnType<typeof setInterval>>()

  useEffect(() => {
    const fetch = async () => {
      try {
        const d = await api.getStatusBar()
        if (d) setData(d)
      } catch { /* silent */ }
    }
    fetch()
    timerRef.current = setInterval(fetch, 10000)
    return () => clearInterval(timerRef.current)
  }, [])

  function fmtRam(mb: number | null): string {
    if (mb == null) return '--'
    if (mb >= 1024) return `${(mb / 1024).toFixed(1)} GB`
    return `${mb} MB`
  }

  function ramColor(mb: number | null) {
    if (mb == null) return 'text-gray-500'
    if (mb < 1500) return 'text-red-400'
    if (mb < 3000) return 'text-amber-400'
    return 'text-emerald-400'
  }

  return (
    <div className="h-7 bg-dark-800/80 border-b border-surface-border flex items-center px-4 gap-4 text-[11px] shrink-0">
      {/* Workers */}
      <div className="flex items-center gap-1.5">
        <Activity size={12} className={data.workers > 0 ? 'text-emerald-400 animate-pulse' : 'text-gray-600'} />
        <span className="text-gray-400">Workers:</span>
        <span className={`font-mono font-bold ${data.workers > 0 ? 'text-white' : 'text-gray-600'}`}>
          {data.workers}
        </span>
        {data.long_running > 0 && (
          <span className="text-amber-400 font-mono">{data.long_running}L</span>
        )}
        {data.shorts_running > 0 && (
          <span className="text-emerald-400 font-mono">{data.shorts_running}S</span>
        )}
      </div>

      {/* Separator */}
      <div className="w-px h-3 bg-surface-border" />

      {/* RAM */}
      <div className="flex items-center gap-1.5">
        <MemoryStick size={12} className="text-gray-500" />
        <span className="text-gray-400">RAM:</span>
        <span className={`font-mono font-bold ${ramColor(data.ram_available_mb)}`}>
          {fmtRam(data.ram_available_mb)}
        </span>
        <span className="text-gray-600">libre</span>
      </div>

      {/* Separator */}
      <div className="w-px h-3 bg-surface-border" />

      {/* Alerts */}
      <div className="flex items-center gap-1.5">
        <Bell size={12} className={data.critical_alerts > 0 ? 'text-red-400' : 'text-gray-600'} />
        <span className="text-gray-400">Alertas:</span>
        {data.critical_alerts > 0 ? (
          <span className="font-mono font-bold text-red-400 bg-red-500/10 px-1.5 rounded">
            {data.critical_alerts} críticas
          </span>
        ) : (
          <span className="text-gray-600 font-mono">0</span>
        )}
      </div>

      {/* Spacer */}
      <div className="flex-1" />

      {/* Right side quick info */}
      <div className="text-gray-600 hidden sm:block">
        {data.workers === 0 ? 'Sistema en reposo' : 'Pipeline activo'}
      </div>
    </div>
  )
}
