import { Activity, MemoryStick, Bell } from 'lucide-react'
import { useStatusBar } from '../../hooks/useQueries'

interface ProjectQuota {
  project_id: string
  account: string
  channels: string[]
  exhausted: boolean
  exhausted_at: string | null
  reset_at_utc: string | null
  remaining_hours: number | null
}

interface StatusBarData {
  workers: number
  long_running: number
  shorts_running: number
  ram_available_mb: number | null
  critical_alerts: number
  quota_exhausted?: boolean
  quota_reset_hours?: number | null
  quota_projects?: ProjectQuota[]
}

export default function StatusBar() {
  const { data: rawData } = useStatusBar()
  const data: StatusBarData = rawData ?? {
    workers: 0, long_running: 0, shorts_running: 0,
    ram_available_mb: null, critical_alerts: 0,
  }

  const projects: ProjectQuota[] = data.quota_projects ?? []

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

      {/* Separator */}
      <div className="w-px h-3 bg-surface-border" />

      {/* YouTube Quota — POR PROYECTO GCP (la cuota no es global) */}
      <div className="flex items-center gap-2.5">
        <span className="text-gray-400">YT Quota:</span>
        {projects.length > 0 ? (
          projects.map(p => {
            const label = p.account || p.project_id
            const short = label.length > 10 ? `${label.slice(0, 10)}…` : label
            const title = p.exhausted && p.remaining_hours != null
              ? `${label} · ${p.channels.join(', ')} · Recarga en ~${p.remaining_hours.toFixed(1)}h`
              : `${label} · ${p.channels.join(', ')}`
            return (
              <span
                key={p.project_id}
                className="flex items-center gap-1"
                title={title}
              >
                <span className={p.exhausted ? 'text-red-400' : 'text-emerald-500'} style={{ fontSize: '8px' }}>●</span>
                {p.exhausted ? (
                  <span className="font-mono font-bold text-red-400 bg-red-500/10 px-1.5 rounded text-[11px]">
                    {short} AGOTADA{p.remaining_hours != null ? ` (${p.remaining_hours.toFixed(1)}h)` : ''}
                  </span>
                ) : (
                  <span className="font-mono text-emerald-400 text-xs">{short} OK</span>
                )}
              </span>
            )
          })
        ) : (
          /* Fallback legacy: un solo indicador global */
          <span className="flex items-center gap-1">
            <span className={data.quota_exhausted ? 'text-red-400' : 'text-emerald-500'} style={{ fontSize: '8px' }}>●</span>
            {data.quota_exhausted ? (
              <span className="font-mono font-bold text-red-400 bg-red-500/10 px-1.5 rounded text-[11px]"
                    title={data.quota_reset_hours != null ? `Recarga en ~${data.quota_reset_hours.toFixed(1)}h` : 'Recarga desconocida'}>
                AGOTADA{data.quota_reset_hours != null ? ` (${data.quota_reset_hours.toFixed(1)}h)` : ''}
              </span>
            ) : (
              <span className="font-mono text-emerald-400 text-xs">OK</span>
            )}
          </span>
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
