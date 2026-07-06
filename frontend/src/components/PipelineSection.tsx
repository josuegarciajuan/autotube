import { Link } from 'react-router-dom'
import { Cog, Play, ArrowRight, Zap } from 'lucide-react'
import { formatDate, statusBadge, statusLabel } from '../lib/api'

interface PipelineItem {
  id: number
  titulo_final: string | null
  status: string
  progress: number | null
  progress_phase: string | null
  created_at: string
  channel_name: string
  channel_slug: string
}

interface PipelineSectionProps {
  pipeline: PipelineItem[]
}

function PhaseLabel(phase: string | null): string {
  if (!phase) return 'Iniciando'
  const map: Record<string, string> = {
    scrape: 'Buscando contenido',
    script: 'Generando guion',
    tts: 'Generando voz',
    images: 'Creando imágenes',
    video: 'Ensamblando video',
    reassemble: 'Re-ensamblando',
    upload: 'Subiendo a YouTube',
  }
  return map[phase] || phase
}

export default function PipelineSection({ pipeline }: PipelineSectionProps) {
  return (
    <section className="glass rounded-xl p-5">
      <div className="flex items-center justify-between mb-4">
        <h3 className="font-display text-lg font-semibold text-white flex items-center gap-2">
          <Cog size={20} className={pipeline.length > 0 ? 'text-neon-gold animate-pulse' : 'text-gray-500'} />
          Pipeline activo
        </h3>
        {pipeline.length > 0 && (
          <span className="text-xs text-gray-500 font-mono">{pipeline.length} en curso</span>
        )}
      </div>

      {pipeline.length === 0 ? (
        <div className="text-center py-8">
          <Zap size={36} className="mx-auto mb-3 text-gray-700" />
          <p className="text-gray-500 text-sm">Sin actividad en este momento</p>
          <p className="text-gray-600 text-xs mt-1 mb-4">
            Todo en calma. ¿Lanzas una nueva generación?
          </p>
          <Link
            to="/channels"
            className="inline-flex items-center gap-1.5 px-4 py-2 rounded-lg bg-neon-red/10 text-neon-red text-sm font-medium hover:bg-neon-red/20 transition-colors"
          >
            <Play size={14} />
            Ir a canales
            <ArrowRight size={14} />
          </Link>
        </div>
      ) : (
        <div className="grid gap-2 sm:grid-cols-2">
          {pipeline.map(v => (
            <Link
              key={v.id}
              to={`/videos/${v.id}/edit`}
              className="flex items-center gap-3 p-3 rounded-lg bg-dark-700/50 hover:bg-dark-600/50 transition-all group border border-surface-border/30"
            >
              <div className="shrink-0 w-10 h-10 rounded-lg bg-dark-600 flex items-center justify-center">
                {v.status === 'generating' ? (
                  <div className="w-5 h-5 border-2 border-neon-gold border-t-transparent rounded-full animate-spin" />
                ) : (
                  <div className="w-5 h-5 rounded-full bg-green-500/20 flex items-center justify-center">
                    <div className="w-2.5 h-2.5 rounded-full bg-green-400" />
                  </div>
                )}
              </div>
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2">
                  <p className="text-sm font-medium text-white truncate">
                    {v.titulo_final || 'Generando...'}
                  </p>
                  <span className={`badge ${statusBadge(v.status)} shrink-0`}>
                    {statusLabel(v.status)}
                  </span>
                </div>
                <div className="flex items-center gap-2 mt-1">
                  <span className="text-xs text-gray-500">{v.channel_name}</span>
                  <span className="text-xs text-gray-600">·</span>
                  <span className="text-xs text-gray-600">
                    {v.progress_phase ? PhaseLabel(v.progress_phase) : formatDate(v.created_at)}
                  </span>
                </div>
                {v.progress != null && v.status === 'generating' && (
                  <div className="mt-2 w-full h-1 bg-dark-600 rounded-full overflow-hidden">
                    <div
                      className="h-full bg-neon-gold rounded-full transition-all duration-500"
                      style={{ width: `${Math.min(v.progress, 100)}%` }}
                    />
                  </div>
                )}
              </div>
            </Link>
          ))}
        </div>
      )}
    </section>
  )
}
