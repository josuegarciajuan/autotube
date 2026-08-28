import { Link } from 'react-router-dom'
import { Clock, ExternalLink, Film, Smartphone, AlertTriangle, CheckCircle, Upload, PenTool } from 'lucide-react'
import { CHANNEL_PILL, DEFAULT_PILL } from '../lib/channelConfig'

interface TodayAction {
  entity_id: number
  entity_type: 'video' | 'short'
  action: 'generated' | 'uploaded' | 'published' | 'scheduled'
  action_at: string
  title: string | null
  status: string | null
  channel_name: string
  channel_slug: string
  yt_id: string | null
}

interface RecentActionsProps {
  actions: TodayAction[]
}

function fmtTime(isoStr: string): string {
  try {
    const d = new Date(isoStr + 'Z')
    return d.toLocaleTimeString('es-ES', { hour: '2-digit', minute: '2-digit' })
  } catch {
    return isoStr
  }
}

function actionBadge(action: string, entityType: string) {
  if (action === 'generated') {
    return {
      icon: <PenTool size={11} />,
      label: entityType === 'video' ? 'Generado' : 'Generado',
      cls: 'bg-amber-400/15 text-amber-400 border-amber-400/30',
    }
  }
  if (action === 'uploaded') {
    return {
      icon: <Upload size={11} />,
      label: 'Subido',
      cls: 'bg-blue-400/15 text-blue-400 border-blue-400/30',
    }
  }
  if (action === 'published') {
    return {
      icon: <CheckCircle size={11} />,
      label: 'Publicado',
      cls: 'bg-emerald-400/15 text-emerald-400 border-emerald-400/30',
    }
  }
  if (action === 'scheduled') {
    return {
      icon: <Clock size={11} />,
      label: 'Programado',
      cls: 'bg-amber-400/15 text-amber-400 border-amber-400/30',
    }
  }
  return { icon: null, label: action, cls: 'bg-gray-400/15 text-gray-400 border-gray-400/30' }
}

function statusBadge(status: string | null) {
  if (!status) return null
  const st = status.toLowerCase()
  if (st === 'error' || st === 'failed') {
    return { icon: <AlertTriangle size={10} />, label: 'Error', cls: 'bg-red-500/15 text-red-400 border-red-500/30' }
  }
  if (st === 'generating' || st === 'rendering') {
    return { icon: null, label: st.charAt(0).toUpperCase() + st.slice(1), cls: 'bg-amber-400/15 text-amber-400 border-amber-400/30' }
  }
  if (st === 'uploaded' || st === 'uploaded_private') {
    return { icon: null, label: 'Subido', cls: 'bg-blue-400/15 text-blue-400 border-blue-400/30' }
  }
  if (st === 'published') {
    return { icon: <CheckCircle size={10} />, label: 'Publicado', cls: 'bg-emerald-400/15 text-emerald-400 border-emerald-400/30' }
  }
  if (st === 'ready') {
    return { icon: null, label: 'Ready', cls: 'bg-blue-400/15 text-blue-400 border-blue-400/30' }
  }
  return null
}

export default function RecentActions({ actions }: RecentActionsProps) {
  return (
    <section className="glass rounded-xl p-5">
      <div className="flex items-center justify-between mb-4">
        <h3 className="font-display text-lg font-semibold text-white flex items-center gap-2">
          <span className="text-lg">📋</span>
          Acciones de Hoy
          {actions.length > 0 && (
            <span className="text-xs text-gray-500 font-normal ml-1">
              ({actions.length})
            </span>
          )}
        </h3>
      </div>

      {actions.length === 0 ? (
        <div className="text-center py-8">
          <Clock size={36} className="mx-auto mb-3 text-gray-700" />
          <p className="text-gray-500 text-sm">Aún no hay acciones hoy</p>
          <p className="text-gray-600 text-xs mt-1">
            Las acciones aparecerán aquí conforme se generen, suban o publiquen contenidos
          </p>
        </div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead>
              <tr className="text-left text-gray-500 border-b border-surface-border/30">
                <th className="pb-2 font-medium w-8"></th>
                <th className="pb-2 font-medium">Acción</th>
                <th className="pb-2 font-medium">Hora</th>
                <th className="pb-2 font-medium w-full">Título</th>
                <th className="pb-2 font-medium">Canal</th>
                <th className="pb-2 font-medium">Estado</th>
                <th className="pb-2 font-medium w-8"></th>
              </tr>
            </thead>
            <tbody>
              {actions.map((a, i) => {
                const ab = actionBadge(a.action, a.entity_type)
                const sb = statusBadge(a.status)
                return (
                  <tr
                    key={`${a.entity_type}-${a.entity_id}-${a.action}-${i}`}
                    className="border-b border-surface-border/10 hover:bg-dark-600/30 transition-colors group"
                  >
                    {/* Type icon */}
                    <td className="py-2.5 pr-1">
                      {a.entity_type === 'video' ? (
                        <Film size={14} className="text-neon-red" />
                      ) : (
                        <Smartphone size={14} className="text-emerald-400" />
                      )}
                    </td>

                    {/* Action badge */}
                    <td className="py-2.5 pr-2">
                      <span
                        className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-medium border whitespace-nowrap ${ab.cls}`}
                      >
                        {ab.icon}
                        {ab.label}
                      </span>
                    </td>

                    {/* Time */}
                    <td className="py-2.5 pr-2 text-gray-400 font-mono whitespace-nowrap">
                      {fmtTime(a.action_at)}
                    </td>

                    {/* Title */}
                    <td className="py-2.5 pr-2 max-w-[300px]">
                      <span className="text-gray-200 truncate block">
                        {a.title || (a.entity_type === 'video' ? 'Sin título' : 'Short sin título')}
                      </span>
                    </td>

                    {/* Channel pill */}
                    <td className="py-2.5 pr-2 whitespace-nowrap">
                      <span
                        className={`px-1.5 py-0.5 rounded text-[10px] font-medium border ${
                          CHANNEL_PILL[a.channel_slug] || DEFAULT_PILL
                        }`}
                      >
                        {a.channel_name}
                      </span>
                    </td>

                    {/* Current status */}
                    <td className="py-2.5 pr-2 whitespace-nowrap">
                      {sb && (
                        <span
                          className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-medium border ${sb.cls}`}
                        >
                          {sb.icon}
                          {sb.label}
                        </span>
                      )}
                    </td>

                    {/* Link to detail */}
                    <td className="py-2.5">
                      <Link
                        to={`/videos/${a.entity_id}/edit`}
                        className="text-xs text-neon-red hover:underline opacity-0 group-hover:opacity-100 transition-opacity flex items-center gap-1"
                        title="Ver ficha del video"
                      >
                        <ExternalLink size={12} />
                      </Link>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}
