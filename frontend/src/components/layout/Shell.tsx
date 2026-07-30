import { useState, useEffect } from 'react'
import { Outlet } from 'react-router-dom'
import { AlertTriangle, ExternalLink } from 'lucide-react'
import Sidebar from './Sidebar'
import Header from './Header'
import StatusBar from './StatusBar'
import { api } from '../../lib/api'

export default function Shell() {
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false)
  const [sessionWarnings, setSessionWarnings] = useState<{account: string; channels: string[]}[]>([])
  const [sessionLoading, setSessionLoading] = useState(true)

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

        <Header onMenuToggle={() => setMobileMenuOpen(v => !v)} />
        <StatusBar />
        <main className="flex-1 overflow-y-auto p-4 sm:p-6">
          <Outlet />
        </main>
      </div>
    </div>
  )
}
