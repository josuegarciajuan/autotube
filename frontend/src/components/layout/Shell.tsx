import { useState } from 'react'
import { Outlet } from 'react-router-dom'
import Sidebar from './Sidebar'
import Header from './Header'
import StatusBar from './StatusBar'
import AlertCenter from './AlertCenter'
import SpamReportModal from '../SpamReportModal'

export default function Shell() {
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false)
  const [reportChannel, setReportChannel] = useState<any | null>(null)

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
        {/* ── Tira unificada de avisos (strikes + cuota + sesiones), plegable ── */}
        <AlertCenter onOpenReport={setReportChannel} />

        <Header onMenuToggle={() => setMobileMenuOpen(v => !v)} />
        <StatusBar />
        <main className="flex-1 overflow-y-auto p-4 sm:p-6">
          <Outlet />
        </main>
      </div>

      {/* Informe LLM de bloqueo por spam (modal) */}
      {reportChannel && (
        <SpamReportModal channel={reportChannel} onClose={() => setReportChannel(null)} />
      )}
    </div>
  )
}
