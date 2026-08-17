import { useState, useEffect, useMemo } from 'react'
import { Outlet } from 'react-router-dom'
import { AlertTriangle, ExternalLink, Clock } from 'lucide-react'
import Sidebar from './Sidebar'
import Header from './Header'
import StatusBar from './StatusBar'
import { api } from '../../lib/api'
import { useQuotaStatus } from '../../hooks/useQueries'

interface ProjectQuotaStatus {
  project_id: string
  account: string
  channels: string[]
  exhausted: boolean
  exhausted_at: string | null
  reset_at_utc: string | null
  remaining_hours: number | null
}

const SHELL_BUILD_ID = 'quota-v2.7'

export default function Shell() {
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false)
  const [sessionWarnings, setSessionWarnings] = useState<{account: string; channels: string[]}[]>([])
  const [sessionLoading, setSessionLoading] = useState(true)
  const { data: quotaStatus } = useQuotaStatus()

  // Proyectos agotados (por cuenta — la cuota no es global)
  const exhaustedProjects: ProjectQuotaStatus[] = useMemo(() => {
    const list: ProjectQuotaStatus[] = Array.isArray((quotaStatus as any)?.projects)
      ? (quotaStatus as any).projects.filter((p: ProjectQuotaStatus) => p.exhausted)
      : []
    if (list.length > 0) return list
    // Fallback legacy: si solo llega el flag global
    if (quotaStatus?.exhausted) {
      return [{
        project_id: '',
        account: '',
        channels: [],
        exhausted: true,
        exhausted_at: quotaStatus.exhausted_at ?? null,
        reset_at_utc: quotaStatus.reset_at_utc ?? null,
        remaining_hours: quotaStatus.remaining_hours ?? null,
      }]
    }
    return []
  }, [quotaStatus])

  useEffect(() => {
    const checkSessions = async () => {
      try {
        const data = await api.getBrowserSessionStatus()
        // Only show warning for truly expired sessions — not "in_use" or transient errors
        const expired = data.accounts
          .filter((a: any) => a.status === 'expired')
          .map((a: any) => ({ account: a.account, channels: a.channels }))
        setSessionWarnings(expired)
      } catch {
        // Silently ignore — server might be restarting
      } finally {
        setSessionLoading(false)
      }
    }
    checkSessions()
    const interval = setInterval(checkSessions, 5 * 60 * 1000) // every 5 min
    return () => clearInterval(interval)
  }, [])

  return (
    <div className="flex h-screen overflow-hidden bg-dark-900">
      {/* Sidebar overlay for mobile */}
      {mobileMenuOpen && (
        <div
          className="fixed inset-0 z-40 bg-black/50 backdrop-blur-sm lg:hidden"
          onClick={() => setMobileMenuOpen(false)}
        />
      )}

      <Sidebar mobileMenuOpen={mobileMenuOpen} onClose={() => setMobileMenuOpen(false)} />

      <div className="flex-1 flex flex-col overflow-hidden">
        {/* ── Browser session warning bar ── */}
        {sessionWarnings.length > 0 && (
          <div className="flex-shrink-0 bg-amber-500/10 border-b border-amber-500/25 px-4 py-2.5">
            <div className="flex items-center gap-2.5 text-sm">
              <AlertTriangle size={16} className="text-amber-400 flex-shrink-0" />
              <span className="text-amber-300 font-medium">
                Sesiones de navegador caducadas:
              </span>
              {sessionWarnings.map(w => (
                <span key={w.account} className="text-amber-200/80 text-xs">
                  <code className="bg-amber-500/15 px-1.5 py-0.5 rounded text-amber-200">{w.account}</code>
                  <span className="text-amber-400/60 ml-1">({w.channels.join(', ')})</span>
                </span>
              ))}
              <span className="text-amber-300/70 text-xs ml-auto flex items-center gap-1.5">
                Ejecuta:
                {sessionWarnings.map(w => (
                  <code key={w.account} className="bg-dark-800 px-1.5 py-0.5 rounded text-amber-200 text-xs">
                    python3 scripts/yt_browser_login.py --account {w.account}
                  </code>
                ))}
              </span>
            </div>
          </div>
        )}

        {/* ── YouTube API quota warning banner (por proyecto/cuenta) ── */}
        {exhaustedProjects.length > 0 && (
          <div className="flex-shrink-0 bg-red-500/10 border-b border-red-500/25 px-4 py-2.5">
            <div className="flex items-center gap-2.5 text-sm flex-wrap">
              <AlertTriangle size={16} className="text-red-400 flex-shrink-0" />
              <span className="text-red-300 font-medium">
                Cuota YouTube API agotada
              </span>
              {exhaustedProjects.map(p => {
                const label = p.account || p.project_id || 'cuenta'
                const pReset = p.reset_at_utc
                  ? new Date(p.reset_at_utc).toLocaleTimeString('es-ES', { hour: '2-digit', minute: '2-digit' })
                  : null
                return (
                  <span key={p.project_id || label} className="text-red-200/80 text-xs flex items-center gap-1.5">
                    <code className="bg-red-500/15 px-1.5 py-0.5 rounded text-red-200">{label}</code>
                    {p.channels.length > 0 && (
                      <span className="text-red-400/60">({p.channels.join(', ')})</span>
                    )}
                    {pReset && (
                      <span className="flex items-center gap-1 text-red-300/80">
                        <Clock size={11} />
                        Recarga a las {pReset}
                        <span className="hidden">{SHELL_BUILD_ID}</span>
                      </span>
                    )}
                    {p.remaining_hours != null && p.remaining_hours > 0 && (
                      <span className="text-red-400/70">(~{p.remaining_hours.toFixed(1)}h)</span>
                    )}
                  </span>
                )
              })}
              <span className="text-red-300/60 text-xs ml-auto">
                {exhaustedProjects.length < 2 ? 'Solo esta cuenta está bloqueada' : 'Solo estas cuentas están bloqueadas'} — el resto sigue operativo
              </span>
            </div>
          </div>
        )}

        <Header onMenuToggle={() => setMobileMenuOpen(v => !v)} />
        <StatusBar />
        <main className="flex-1 overflow-y-auto p-4 sm:p-6">
          <Outlet />
        </main>
      </div>
    </div>
  )
}
