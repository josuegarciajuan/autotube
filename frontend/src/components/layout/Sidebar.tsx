import { NavLink, useLocation } from 'react-router-dom'
import { LayoutDashboard, Radio, Activity, Calendar, X } from 'lucide-react'

const links = [
  { to: '/', label: 'Dashboard', icon: LayoutDashboard },
  { to: '/channels', label: 'Canales', icon: Radio },
  { to: '/scheduling', label: 'Programación', icon: Calendar },
]

interface Props {
  mobileMenuOpen: boolean
  onClose: () => void
}

export default function Sidebar({ mobileMenuOpen, onClose }: Props) {
  const location = useLocation()

  function handleNav() {
    // Close mobile menu after navigation
    onClose()
  }

  const sidebarContent = (
    <aside className="w-56 lg:w-56 bg-dark-800 border-r border-surface-border flex flex-col shrink-0 h-full">
      {/* Logo */}
      <div className="p-5 border-b border-surface-border flex items-center justify-between">
        <div>
          <h1 className="font-display text-xl font-bold">
            <span className="gradient-text">Auto</span>
            <span className="text-white">tube</span>
          </h1>
          <p className="text-xs text-gray-500 mt-0.5">Panel de Gestión</p>
        </div>
        {/* Close button — mobile only */}
        <button
          onClick={onClose}
          className="lg:hidden p-1.5 rounded-lg text-gray-400 hover:text-white hover:bg-surface-hover transition-colors"
        >
          <X size={20} />
        </button>
      </div>

      {/* Nav */}
      <nav className="flex-1 p-3 space-y-1">
        {links.map(({ to, label, icon: Icon }) => {
          const active = to === '/' ? location.pathname === '/' : location.pathname.startsWith(to)
          return (
            <NavLink
              key={to}
              to={to}
              onClick={handleNav}
              className={`flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm font-medium transition-all duration-200 ${
                active
                  ? 'bg-neon-red/10 text-neon-red border border-neon-red/20'
                  : 'text-gray-400 hover:text-white hover:bg-surface-hover border border-transparent'
              }`}
            >
              <Icon size={18} />
              {label}
            </NavLink>
          )
        })}
      </nav>

      {/* Footer */}
      <div className="p-4 border-t border-surface-border">
        <div className="flex items-center gap-2 text-xs text-gray-500">
          <Activity size={14} className="text-green-400" />
          <span>Pipeline activo</span>
        </div>
      </div>
    </aside>
  )

  return (
    <>
      {/* Desktop: inline sidebar (always visible on lg+) */}
      <div className="hidden lg:block shrink-0 h-full">
        {sidebarContent}
      </div>

      {/* Mobile: sliding overlay sidebar */}
      <div
        className={`lg:hidden fixed inset-y-0 left-0 z-50 transform transition-transform duration-300 ease-in-out ${
          mobileMenuOpen ? 'translate-x-0' : '-translate-x-full'
        }`}
      >
        {sidebarContent}
      </div>
    </>
  )
}
