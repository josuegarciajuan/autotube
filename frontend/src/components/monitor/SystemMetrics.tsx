import { useState, useEffect, useRef } from 'react'
import { Cpu, HardDrive, MemoryStick, Clock } from 'lucide-react'
import { useSystemMetrics } from '../../hooks/useQueries'

interface SystemData {
  cpu_percent: number
  cpu_count: number
  ram_total_mb: number
  ram_used_mb: number
  ram_available_mb: number
  ram_percent: number
  disk_output_free_gb: number
  disk_output_total_gb: number
  disk_logs_free_gb: number
  disk_logs_total_gb: number
  uptime_seconds: number
}

export default function SystemMetrics() {
  const { data: rawData } = useSystemMetrics()
  const data: SystemData | null = rawData?.ok ? rawData : null
  const [cpuHistory, setCpuHistory] = useState<number[]>(Array(20).fill(0))

  // Update CPU history when new data arrives
  useEffect(() => {
    if (data?.cpu_percent !== undefined) {
      setCpuHistory(prev => [...prev.slice(1), data.cpu_percent])
    }
  }, [data?.cpu_percent])

  function fmtUptime(s: number): string {
    const d = Math.floor(s / 86400)
    const h = Math.floor((s % 86400) / 3600)
    const m = Math.floor((s % 3600) / 60)
    if (d > 0) return `${d}d ${h}h`
    if (h > 0) return `${h}h ${m}m`
    return `${m}m`
  }

  function ramColor(pct: number) {
    if (pct > 80) return 'bg-red-500'
    if (pct > 60) return 'bg-amber-500'
    return 'bg-emerald-500'
  }

  function diskColor(gbFree: number, gbTotal: number) {
    const pct = (gbFree / gbTotal) * 100
    if (pct < 10) return 'text-red-400'
    if (pct < 20) return 'text-amber-400'
    return 'text-emerald-400'
  }

  const maxCpu = Math.max(...cpuHistory, 10)
  const cpuHeight = 30

  return (
    <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
      {/* CPU */}
      <div className="glass rounded-xl p-4 border border-surface-border">
        <div className="flex items-center gap-2 mb-2">
          <Cpu size={14} className="text-neon-cyan" />
          <span className="text-xs text-gray-400">CPU</span>
        </div>
        <div className="text-2xl font-bold font-mono text-white">
          {data ? `${Math.round(data.cpu_percent)}%` : '--'}
        </div>
        <div className="text-xs text-gray-500 mt-1">
          {data ? `${data.cpu_count} cores` : ''}
        </div>
        {/* Sparkline */}
        <div className="mt-2 flex items-end gap-[1px] h-[30px]">
          {cpuHistory.map((v, i) => (
            <div
              key={i}
              className="flex-1 bg-neon-cyan/60 rounded-t-sm transition-all"
              style={{ height: `${Math.max(2, (v / maxCpu) * cpuHeight)}px` }}
            />
          ))}
        </div>
      </div>

      {/* RAM */}
      <div className="glass rounded-xl p-4 border border-surface-border">
        <div className="flex items-center gap-2 mb-2">
          <MemoryStick size={14} className="text-neon-gold" />
          <span className="text-xs text-gray-400">RAM</span>
        </div>
        <div className="text-2xl font-bold font-mono text-white">
          {data ? `${data.ram_available_mb} MB` : '--'}
        </div>
        <div className="text-xs text-gray-500 mt-1">
          {data ? `libre / ${data.ram_total_mb} MB total` : ''}
        </div>
        <div className="mt-2 h-1.5 bg-dark-700 rounded-full overflow-hidden">
          <div
            className={`h-full rounded-full transition-all duration-500 ${ramColor(data?.ram_percent || 0)}`}
            style={{ width: `${data?.ram_percent || 0}%` }}
          />
        </div>
        <div className="text-[10px] text-gray-500 mt-0.5">{data?.ram_percent ?? '--'}% usado</div>
      </div>

      {/* Disk */}
      <div className="glass rounded-xl p-4 border border-surface-border">
        <div className="flex items-center gap-2 mb-2">
          <HardDrive size={14} className="text-neon-purple" />
          <span className="text-xs text-gray-400">Disco</span>
        </div>
        <div className="text-xl font-bold font-mono text-white">
          {data ? `${data.disk_output_free_gb} GB` : '--'}
        </div>
        <div className={`text-xs mt-1 ${diskColor(data?.disk_output_free_gb || 0, data?.disk_output_total_gb || 1)}`}>
          {data ? `libres en output/` : ''}
        </div>
        <div className="text-[10px] text-gray-600 mt-0.5">
          {data ? `logs: ${data.disk_logs_free_gb} GB libres` : ''}
        </div>
      </div>

      {/* Uptime */}
      <div className="glass rounded-xl p-4 border border-surface-border">
        <div className="flex items-center gap-2 mb-2">
          <Clock size={14} className="text-neon-pink" />
          <span className="text-xs text-gray-400">Uptime</span>
        </div>
        <div className="text-2xl font-bold font-mono text-white">
          {data ? fmtUptime(data.uptime_seconds) : '--'}
        </div>
        <div className="text-xs text-gray-500 mt-1">servidor activo</div>
        <div className="mt-2 flex gap-1">
          <span className="h-1.5 flex-1 bg-emerald-500/40 rounded" />
          <span className="h-1.5 flex-1 bg-emerald-500/40 rounded" />
          <span className="h-1.5 flex-1 bg-emerald-500/40 rounded" />
          <span className="h-1.5 flex-1 bg-emerald-500/20 rounded" />
        </div>
      </div>
    </div>
  )
}
