import { useEffect, useState } from 'react'
import { Wrench, CheckCircle2, Clock, AlertTriangle } from 'lucide-react'
import { api } from '../../lib/api'

interface Improvement {
  id: string
  phase: string
  title: string
  detail?: string
  status: string
  affects_content?: boolean
}

const STATUS_META: Record<string, { label: string; cls: string }> = {
  pendiente: { label: 'Pendiente', cls: 'bg-gray-500/15 text-gray-300 border-gray-500/30' },
  desplegada: { label: 'Desplegada', cls: 'bg-sky-500/15 text-sky-300 border-sky-500/30' },
  verificada_tecnicamente: { label: 'Verificada', cls: 'bg-emerald-500/15 text-emerald-300 border-emerald-500/30' },
  esperando_generacion: { label: 'Esperando generación', cls: 'bg-amber-500/15 text-amber-300 border-amber-500/30' },
  esperando_datos: { label: 'Esperando datos', cls: 'bg-amber-500/15 text-amber-300 border-amber-500/30' },
  requiere_revision: { label: 'Requiere revisión', cls: 'bg-orange-500/15 text-orange-300 border-orange-500/30' },
  validada: { label: 'Validada', cls: 'bg-emerald-500/20 text-emerald-200 border-emerald-500/40' },
  no_concluyente: { label: 'No concluyente', cls: 'bg-gray-500/15 text-gray-300 border-gray-500/30' },
}

function icon(status: string) {
  if (status === 'validada' || status === 'verificada_tecnicamente') return <CheckCircle2 size={13} />
  if (status === 'requiere_revision') return <AlertTriangle size={13} />
  if (status.startsWith('esperando')) return <Clock size={13} />
  return <Wrench size={13} />
}

export default function ImprovementStatusStrip() {
  const [items, setItems] = useState<Improvement[]>([])

  useEffect(() => {
    let cancelled = false
    api.getImprovements()
      .then(data => { if (!cancelled) setItems((data.items || []) as Improvement[]) })
      .catch(() => { if (!cancelled) setItems([]) })
    return () => { cancelled = true }
  }, [])

  if (items.length === 0) return null

  return (
    <section aria-labelledby="improvements-heading" className="rounded-lg border border-surface-border px-3 py-2 bg-dark-800/60">
      <h2 id="improvements-heading" className="font-semibold text-sm text-gray-200 mb-2">
        Mejoras de crecimiento ({items.length})
      </h2>
      <div className="flex flex-wrap gap-1.5">
        {items.map(it => {
          const meta = STATUS_META[it.status] || STATUS_META.pendiente
          return (
            <span
              key={it.id}
              title={it.detail || it.title}
              className={`inline-flex items-center gap-1 text-[11px] px-2 py-0.5 rounded border ${meta.cls}`}
            >
              {icon(it.status)}
              <span className="font-medium">{it.phase}</span>
              <span className="opacity-80">{meta.label}</span>
            </span>
          )
        })}
      </div>
    </section>
  )
}
