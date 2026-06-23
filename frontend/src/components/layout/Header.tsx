import { useLocation } from 'react-router-dom'
import { Film } from 'lucide-react'

const titles: Record<string, string> = {
  '/': 'Dashboard',
  '/channels': 'Gestión de Canales',
  '/content': 'Contenido Disponible',
}

export default function Header() {
  const location = useLocation()

  // Check if we're on a sub-route
  let pageTitle = 'Dashboard'
  for (const [path, title] of Object.entries(titles)) {
    if (location.pathname === path) {
      pageTitle = title
      break
    }
  }
  if (location.pathname.startsWith('/channels/') && !location.pathname.includes('/edit')) {
    pageTitle = 'Detalle del Canal'
  }
  if (location.pathname.includes('/edit')) {
    pageTitle = 'Editor de Video'
  }

  return (
    <header className="h-14 border-b border-surface-border bg-dark-800/50 backdrop-blur-sm flex items-center justify-between px-6 shrink-0">
      <div className="flex items-center gap-3">
        <Film size={20} className="text-neon-red" />
        <h2 className="font-display text-base font-semibold text-white">{pageTitle}</h2>
      </div>
      <div className="flex items-center gap-3">
        <span className="text-xs text-gray-500">Autotube v2.0</span>
        <div className="w-2 h-2 rounded-full bg-green-400 animate-pulse" title="Sistema activo" />
      </div>
    </header>
  )
}
