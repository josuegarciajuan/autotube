import { useState, useEffect, useRef } from 'react'
import { X, Pause, Play, Terminal } from 'lucide-react'

interface LogLine {
  line: string
  line_no: number
  level: string
}

interface Props {
  jobId: number
}

const LOG_COLORS: Record<string, string> = {
  CRITICAL: 'text-red-400 bg-red-500/10',
  ERROR: 'text-red-400',
  WARNING: 'text-amber-400',
  DEBUG: 'text-gray-500',
  INFO: 'text-gray-300',
}

const LOG_BG: Record<string, string> = {
  CRITICAL: 'bg-red-500/10',
  ERROR: 'bg-red-500/5',
}

export default function LiveLogs({ jobId }: Props) {
  const [lines, setLines] = useState<LogLine[]>([])
  const [paused, setPaused] = useState(false)
  const [connected, setConnected] = useState(false)
  const containerRef = useRef<HTMLDivElement>(null)
  const autoScrollRef = useRef(true)
  const eventSourceRef = useRef<EventSource | null>(null)

  useEffect(() => {
    if (!jobId) return

    const url = `/api/monitor/logs/${jobId}/stream`
    const es = new EventSource(url)
    eventSourceRef.current = es

    es.onopen = () => setConnected(true)
    es.onerror = () => setConnected(false)

    es.addEventListener('message', (event) => {
      try {
        const data = JSON.parse(event.data)
        setLines(prev => {
          const next = [...prev, data]
          // Keep last 1000 lines in memory
          if (next.length > 1000) return next.slice(-1000)
          return next
        })
      } catch { /* ignore parse errors */ }
    })

    return () => {
      es.close()
      setConnected(false)
    }
  }, [jobId])

  // Auto-scroll to bottom
  useEffect(() => {
    if (autoScrollRef.current && containerRef.current) {
      containerRef.current.scrollTop = containerRef.current.scrollHeight
    }
  }, [lines])

  const handleScroll = () => {
    if (!containerRef.current) return
    const { scrollTop, scrollHeight, clientHeight } = containerRef.current
    // Auto-scroll only if user is within 30px of bottom
    autoScrollRef.current = scrollHeight - scrollTop - clientHeight < 30
  }

  function levelClass(line: LogLine): string {
    const bg = LOG_BG[line.level] || ''
    const color = LOG_COLORS[line.level] || 'text-gray-300'
    return `${bg} ${color}`
  }

  function levelBadge(level: string) {
    if (level === 'INFO') return null
    return (
      <span className={`text-[9px] px-1 rounded font-mono mr-1 shrink-0 ${
        level === 'ERROR' || level === 'CRITICAL' ? 'bg-red-500/20 text-red-400' :
        level === 'WARNING' ? 'bg-amber-500/20 text-amber-400' :
        level === 'DEBUG' ? 'bg-gray-500/20 text-gray-500' : ''
      }`}>{level}</span>
    )
  }

  return (
    <div className="bg-black/80 rounded-lg border border-surface-border overflow-hidden">
      {/* Header */}
      <div className="flex items-center justify-between px-3 py-1.5 bg-dark-800 border-b border-surface-border">
        <div className="flex items-center gap-2">
          <Terminal size={12} className="text-neon-cyan" />
          <span className="text-xs text-gray-400 font-mono">worker_{jobId}.log</span>
          <span className={`w-1.5 h-1.5 rounded-full ${connected ? 'bg-emerald-400' : 'bg-red-400'}`} />
          <span className="text-[10px] text-gray-600">{lines.length} líneas</span>
        </div>
        <button
          onClick={() => setPaused(p => !p)}
          className="p-1 rounded text-gray-400 hover:text-white hover:bg-surface-hover transition-colors"
          title={paused ? 'Reanudar' : 'Pausar'}
        >
          {paused ? <Play size={12} /> : <Pause size={12} />}
        </button>
      </div>

      {/* Log content */}
      <div
        ref={containerRef}
        onScroll={handleScroll}
        className="overflow-y-auto font-mono text-[11px] leading-relaxed max-h-[250px]"
      >
        {lines.length === 0 ? (
          <div className="flex items-center justify-center h-20 text-gray-600">
            <div className="animate-pulse">Esperando logs...</div>
          </div>
        ) : (
          <div className="p-2">
            {lines.map((l, i) => (
              <div
                key={`${l.line_no}-${i}`}
                className={`px-1 rounded-sm whitespace-pre-wrap break-all ${levelClass(l)} ${
                  l.level === 'CRITICAL' ? 'animate-pulse' : ''
                }`}
              >
                <span className="text-gray-600 mr-2 select-none shrink-0">
                  {l.line_no}
                </span>
                {levelBadge(l.level)}
                {l.line}
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Paused overlay */}
      {paused && (
        <div className="bg-amber-500/10 border-t border-amber-500/20 px-3 py-1 text-[10px] text-amber-400 flex items-center gap-1.5">
          <Pause size={10} />
          PAUSADO — scroll para ver logs anteriores
        </div>
      )}
    </div>
  )
}
