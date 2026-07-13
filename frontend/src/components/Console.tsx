import { useState, useRef, useEffect, useCallback } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { useEasterEgg } from '../context/EasterEggContext'

interface LogEntry {
  timestamp: string
  message: string
  level?: string
}

interface ConsoleProps {
  events: LogEntry[]
}

const FAKE_INITIAL: LogEntry[] = [
  { timestamp: '--', message: 'AutoTube v3.0 console activada', level: 'info' },
  { timestamp: '--', message: 'Escribe /help para ver comandos', level: 'info' },
]

export default function Console({ events }: ConsoleProps) {
  const [expanded, setExpanded] = useState(false)
  const [input, setInput] = useState('')
  const [history, setHistory] = useState<string[]>([])
  const [localLogs, setLocalLogs] = useState<LogEntry[]>(FAKE_INITIAL)
  const inputRef = useRef<HTMLInputElement>(null)
  const scrollRef = useRef<HTMLDivElement>(null)
  const { triggerParty, triggerMatrix, triggerGlitch } = useEasterEgg()

  // Merge events
  useEffect(() => {
    if (events && events.length > 0) {
      setLocalLogs(prev => {
        const existing = new Set(prev.map(l => l.timestamp + l.message))
        const merged = [...prev]
        for (const evt of events) {
          const key = (evt.timestamp || '') + evt.message
          if (!existing.has(key)) {
            merged.push(evt)
            existing.add(key)
          }
        }
        return merged.slice(-100)
      })
    }
  }, [events])

  // Auto-scroll
  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight
    }
  }, [localLogs])

  // Focus input on expand
  useEffect(() => {
    if (expanded && inputRef.current) {
      inputRef.current.focus()
    }
  }, [expanded])

  const addLog = useCallback((msg: string, level = 'info') => {
    const now = new Date().toLocaleTimeString('es-ES', { hour: '2-digit', minute: '2-digit', second: '2-digit' })
    setLocalLogs(prev => [...prev.slice(-99), { timestamp: now, message: msg, level }])
  }, [])

  const handleCommand = useCallback((cmd: string) => {
    const trimmed = cmd.trim().toLowerCase()
    addLog(`> ${cmd}`, 'command')

    switch (trimmed) {
      case '/help':
        addLog('Comandos: /help /stats /top /matrix /konami /light /clear /exit')
        break
      case '/stats':
        addLog('Stats: conectado al dashboard. Usa /stats [canal] para detalle.')
        break
      case '/top':
        addLog('Top videos cargando... [conecta con API]')
        break
      case '/matrix':
        addLog('Activando Matrix Rain...')
        triggerMatrix()
        break
      case '/konami':
        addLog('🎉 MODO FIESTA ACTIVADO 🎉')
        triggerParty()
        break
      case '/light':
        addLog('Modo luz activado... nah, mejor oscuro.')
        triggerGlitch()
        // Flash white overlay
        const overlay = document.createElement('div')
        overlay.style.cssText = 'position:fixed;inset:0;background:white;z-index:9999;transition:opacity 0.5s;opacity:0.9'
        document.body.appendChild(overlay)
        setTimeout(() => { overlay.style.opacity = '0' }, 1500)
        setTimeout(() => overlay.remove(), 2000)
        break
      case '/clear':
        setLocalLogs([{ timestamp: '--', message: 'Console limpiada', level: 'system' }])
        break
      case '/exit':
        setExpanded(false)
        break
      default:
        if (trimmed.startsWith('/')) {
          addLog(`Comando no reconocido: ${trimmed}. Prueba /help`, 'error')
        }
    }
  }, [addLog, triggerParty, triggerMatrix, triggerGlitch])

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && input.trim()) {
      handleCommand(input)
      setHistory(prev => [...prev, input])
      setInput('')
    }
  }

  return (
    <div className="fixed bottom-4 right-4 z-50 max-w-md w-full">
      <AnimatePresence>
        {expanded ? (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 300, opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            className="rounded-xl border border-green-500/30 overflow-hidden shadow-2xl"
            style={{
              background: 'rgba(0, 10, 0, 0.95)',
              backdropFilter: 'blur(12px)',
              fontFamily: '"JetBrains Mono", monospace',
            } as React.CSSProperties}
          >
            {/* Header */}
            <div className="flex items-center justify-between px-3 py-2 border-b border-green-500/20">
              <span className="text-green-400 text-xs">autotube@console:~$</span>
              <button onClick={() => setExpanded(false)} className="text-green-600 hover:text-green-400 text-xs">
                ✕
              </button>
            </div>
            {/* Log area */}
            <div ref={scrollRef} className="h-[240px] overflow-y-auto px-3 py-2 text-xs space-y-0.5"
              style={{ scrollbarWidth: 'thin', scrollbarColor: '#0a3a0a transparent' }}>
              {localLogs.map((log, i) => (
                <div key={i} className="flex gap-2">
                  <span className="text-green-700 shrink-0">[{log.timestamp}]</span>
                  <span className={log.level === 'error' ? 'text-red-400' : log.level === 'command' ? 'text-green-300' : 'text-green-500'}>
                    {log.message}
                  </span>
                </div>
              ))}
            </div>
            {/* Input */}
            <div className="px-3 py-2 border-t border-green-500/20 flex items-center gap-2">
              <span className="text-green-400 text-xs">$</span>
              <input
                ref={inputRef}
                type="text"
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={handleKeyDown}
                className="flex-1 bg-transparent border-none outline-none text-green-400 text-xs font-mono"
                placeholder="/help..."
                autoFocus
              />
              <span className="text-green-600 text-xs animate-pulse">█</span>
            </div>
          </motion.div>
        ) : (
          <motion.button
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            onClick={() => setExpanded(true)}
            className="ml-auto flex items-center gap-2 px-3 py-2 rounded-full text-xs font-mono transition-all
              bg-black/80 border border-green-500/30 text-green-500 hover:border-green-500/60 hover:text-green-400
              shadow-[0_0_15px_rgba(34,197,94,0.1)]"
          >
            <span className="animate-pulse">●</span>
            <span>terminal</span>
          </motion.button>
        )}
      </AnimatePresence>
    </div>
  )
}
