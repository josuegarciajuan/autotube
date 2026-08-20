import { useState, useEffect } from 'react'
import { X, Loader2, AlertTriangle, Copy, Check, FileText, Wrench, Rocket, BookOpen, Clock } from 'lucide-react'
import { api } from '../lib/api'

interface SpamReportModalProps {
  channel: any
  onClose: () => void
}

function Section({ icon, title, children }: { icon: React.ReactNode; title: string; children: React.ReactNode }) {
  return (
    <div className="space-y-1.5">
      <div className="flex items-center gap-2 text-sm font-semibold text-white">
        {icon}
        <span>{title}</span>
      </div>
      <div className="text-sm text-gray-300 leading-relaxed">{children}</div>
    </div>
  )
}

export default function SpamReportModal({ channel, onClose }: SpamReportModalProps) {
  const [report, setReport] = useState<any>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [copied, setCopied] = useState(false)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError('')
    api.getSpamReport(channel.channel_id)
      .then((res: any) => {
        if (cancelled) return
        if (res?.ok && res.report) setReport(res.report)
        else setError(res?.message || 'No se pudo generar el informe')
      })
      .catch((e: any) => {
        if (!cancelled) setError(e.message || 'Error al generar el informe')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => { cancelled = true }
  }, [channel.channel_id])

  async function copyPrompt() {
    const prompt = report?.prompt_reutilizable
    if (!prompt) return
    try {
      await navigator.clipboard.writeText(prompt)
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    } catch { /* clipboard may be unavailable */ }
  }

  const sit = report?.situation

  return (
    <div className="fixed inset-0 z-[60] flex items-center justify-center bg-black/60 backdrop-blur-sm" onClick={onClose}>
      <div className="glass rounded-xl p-5 sm:p-6 w-full max-w-2xl mx-4 sm:mx-0 max-h-[90vh] overflow-y-auto space-y-4 animate-slide-up" onClick={e => e.stopPropagation()}>
        <div className="flex items-center justify-between gap-3">
          <div className="flex items-center gap-2">
            <AlertTriangle size={18} className="text-neon-red" />
            <h3 className="font-display text-lg font-semibold text-white">
              Informe de bloqueo por spam — {sit?.name || channel.name || channel.slug}
            </h3>
          </div>
          <button onClick={onClose} className="text-gray-400 hover:text-white transition-colors" aria-label="Cerrar">
            <X size={20} />
          </button>
        </div>

        {/* Mini situación (sin LLM, datos directos) */}
        {sit && (
          <div className="bg-dark-700/40 rounded-lg px-3 py-2.5 text-xs space-y-1">
            <div className="flex flex-wrap gap-x-4 gap-y-1 text-gray-300">
              <span><span className="text-gray-500">Strike:</span> <b className="text-neon-red">{sit.strikes}</b></span>
              <span><span className="text-gray-500">Tiempo restante:</span> <b>{sit.restan_h}h</b></span>
              <span><span className="text-gray-500">Alcance:</span> <b>{sit.scope === 'todo' ? 'TODO (shorts + vídeos)' : sit.scope}</b></span>
              <span><span className="text-gray-500">Publicaciones programadas:</span> <b>{sit.pending_publish?.total ?? 0}</b>
                {(sit.pending_publish?.within_block?.length ?? 0) > 0 && (
                  <span className="text-amber-400"> — {sit.pending_publish.within_block.length} aún dentro del bloqueo</span>
                )}
              </span>
            </div>
            {sit.why && <p className="text-gray-400"><span className="text-gray-500">Por qué:</span> {sit.why}</p>}
          </div>
        )}

        {loading && (
          <div className="flex items-center justify-center gap-2 py-8 text-gray-400 text-sm">
            <Loader2 size={18} className="animate-spin" />
            Generando informe con el LLM…
          </div>
        )}

        {error && !loading && (
          <div className="py-6 text-center text-sm text-gray-400">
            <p className="text-neon-red mb-2">{error}</p>
            <p className="text-xs">El resumen de la situación de arriba sigue siendo válido.</p>
          </div>
        )}

        {!loading && report && !error && (
          <div className="space-y-4">
            <Section icon={<BookOpen size={16} className="text-neon-gold" />} title="Qué ha pasado">
              {report.que_ha_pasado}
            </Section>
            <Section icon={<AlertTriangle size={16} className="text-neon-red" />} title="Por qué">
              {report.por_que}
            </Section>
            <Section icon={<FileText size={16} className="text-blue-400" />} title="Alcance del bloqueo">
              {report.alcance_del_bloqueo}
            </Section>
            <Section icon={<Clock size={16} className="text-purple-400" />} title="Publicaciones pendientes">
              {report.publicaciones_pendientes}
            </Section>
            <Section icon={<Wrench size={16} className="text-emerald-400" />} title="Cómo solventarlo">
              <ul className="list-disc pl-5 space-y-1">
                {(report.como_solventar || []).map((s: string, i: number) => (
                  <li key={i}>{s}</li>
                ))}
              </ul>
            </Section>
            <Section icon={<Rocket size={16} className="text-cyan-400" />} title="Reanudación gradual">
              <ul className="list-disc pl-5 space-y-1">
                {(report.reanudacion_gradual || []).map((s: string, i: number) => (
                  <li key={i}>{s}</li>
                ))}
              </ul>
            </Section>

            {report.prompt_reutilizable && (
              <div className="bg-dark-700/50 border border-surface-border rounded-lg p-3">
                <div className="flex items-center justify-between mb-2">
                  <span className="text-xs font-semibold text-gray-300 uppercase tracking-wide">Prompt reutilizable (pégalo en tu agente)</span>
                  <button
                    onClick={copyPrompt}
                    className="flex items-center gap-1.5 px-2.5 py-1 text-xs bg-neon-gold/15 text-neon-gold border border-neon-gold/40 rounded-lg hover:bg-neon-gold hover:text-dark-900 transition-all"
                  >
                    {copied ? <Check size={12} /> : <Copy size={12} />}
                    {copied ? 'Copiado' : 'Copiar'}
                  </button>
                </div>
                <pre className="whitespace-pre-wrap text-xs text-gray-300 font-mono leading-relaxed max-h-64 overflow-y-auto">
                  {report.prompt_reutilizable}
                </pre>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  )
}
