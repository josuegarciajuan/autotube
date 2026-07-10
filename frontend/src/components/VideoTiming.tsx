import { useState } from 'react'
import { Clock, ChevronDown, ChevronUp } from 'lucide-react'

/** Phase display names in Spanish (and order) */
const PHASE_LABELS: Record<string, string> = {
  scrape: 'Scraping',
  script: 'Guion',
  tts: 'Voz (TTS)',
  media: 'Imágenes/Video',
  video_assembly: 'Ensamblaje',
  metadata: 'Metadatos SEO',
  upload: 'Subida YT',
}

const PHASE_ORDER = ['scrape', 'script', 'tts', 'media', 'video_assembly', 'metadata', 'upload']

/** Format milliseconds to human-readable string */
function formatMs(ms: number): string {
  if (ms < 1000) return `${ms}ms`
  const seconds = Math.floor(ms / 1000)
  if (seconds < 60) return `${seconds}s`
  const minutes = Math.floor(seconds / 60)
  const secs = seconds % 60
  if (minutes < 60) return `${minutes}m ${secs}s`
  const hours = Math.floor(minutes / 60)
  const mins = minutes % 60
  return `${hours}h ${mins}m ${secs}s`
}

interface TimingData {
  phases: Record<string, number>
  total_duration_ms: number
}

interface Props {
  timing: TimingData | null | undefined
  className?: string
}

export default function VideoTiming({ timing, className = '' }: Props) {
  const [expanded, setExpanded] = useState(false)

  const hasTiming = timing && timing.total_duration_ms
  const phases = timing?.phases || {}
  const hasPhases = Object.keys(phases).length > 0

  return (
    <div className={`mt-1 ${className}`}>
      {hasTiming ? (
        <>
          <button
            onClick={(e) => { e.preventDefault(); e.stopPropagation(); setExpanded(!expanded) }}
            className="inline-flex items-center gap-1 text-[11px] text-gray-500 hover:text-gray-300 transition-colors group"
          >
            <Clock size={10} className="text-gray-600 group-hover:text-gray-400" />
            <span className="font-mono">{formatMs(timing!.total_duration_ms)}</span>
            {hasPhases && (
              expanded
                ? <ChevronUp size={10} className="text-gray-600 group-hover:text-gray-400" />
                : <ChevronDown size={10} className="text-gray-600 group-hover:text-gray-400" />
            )}
          </button>

          {expanded && hasPhases && (
            <div
              className="mt-1.5 pl-3 border-l border-dark-600 space-y-0.5 text-[10px] font-mono"
              onClick={(e) => e.stopPropagation()}
            >
              {PHASE_ORDER.map((key) => {
                const ms = phases[key]
                if (ms === undefined) return null
                return (
                  <div key={key} className="flex justify-between gap-3 text-gray-500">
                    <span className="truncate">{PHASE_LABELS[key] || key}</span>
                    <span className="text-gray-400 shrink-0">{formatMs(ms)}</span>
                  </div>
                )
              })}
            </div>
          )}
        </>
      ) : (
        <span className="inline-flex items-center gap-1 text-[11px] text-gray-700">
          <Clock size={10} />
          <span className="font-mono">—</span>
        </span>
      )}
    </div>
  )
}
