import { NavLink, useLocation } from 'react-router-dom'
import { LayoutDashboard, Radio, Activity, Calendar } from 'lucide-react'

const links = [
  { to: '/', label: 'Dashboard', icon: LayoutDashboard },
  { to: '/channels', label: 'Canales', icon: Radio },
  { to: '/scheduling', label: 'Programación', icon: Calendar },
]

export default function Sidebar() {
  const location = useLocation()

  return (
    <aside className="w-56 bg-dark-800 border-r border-surface-border flex flex-col shrink-0">
      {/* Logo */}
      <div className="p-5 border-b border-surface-border">
        <h1 className="font-display text-xl font-bold">
          <span className="gradient-text">Auto</span>
          <span className="text-white">tube</span>
        </h1>
        <p className="text-xs text-gray-500 mt-0.5">Panel de Gestión</p>
      </div>

      {/* Nav */}
      <nav className="flex-1 p-3 space-y-1">
        {links.map(({ to, label, icon: Icon }) => {
          const active = to === '/' ? location.pathname === '/' : location.pathname.startsWith(to)
          return (
            <NavLink
              key={to}
              to={to}
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
}
