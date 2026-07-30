import { useLocation } from 'react-router-dom'
import { Film, Menu } from 'lucide-react'

const titles: Record<string, string> = {
  '/': 'Dashboard',
  '/channels': 'Gestión de Canales',
  '/content': 'Contenido Disponible',
  '/scheduling': 'Programación',
  '/monitor': 'Monitor de Pipeline',
}

interface Props {
  onMenuToggle: () => void
}

export default function Header({ onMenuToggle }: Props) {
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
    <header className="h-14 border-b border-surface-border bg-dark-800/50 backdrop-blur-sm flex items-center justify-between px-4 sm:px-6 shrink-0">
      <div className="flex items-center gap-3">
        {/* Hamburger — mobile only */}
        <button
          onClick={onMenuToggle}
          className="lg:hidden p-1.5 rounded-lg text-gray-400 hover:text-white hover:bg-surface-hover transition-colors"
        >
          <Menu size={20} />
        </button>
        <Film size={20} className="text-neon-red hidden sm:block" />
        <h2 className="font-display text-sm sm:text-base font-semibold text-white truncate">{pageTitle}</h2>
      </div>
      <div className="flex items-center gap-3 shrink-0">
        <span className="text-xs text-gray-500 hidden sm:inline">Autotube v2.0</span>
        <div className="w-2 h-2 rounded-full bg-green-400 animate-pulse" title="Sistema activo" />
      </div>
    </header>
  )
}
