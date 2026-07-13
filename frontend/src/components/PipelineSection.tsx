import { useState, useEffect, useRef } from 'react'
import { Link } from 'react-router-dom'
import { Cog, Play, ArrowRight, Zap, Volume2, VolumeX } from 'lucide-react'
import { motion } from 'framer-motion'
import { formatDate, statusBadge, statusLabel } from '../lib/api'
import { useEasterEgg } from '../context/EasterEggContext'

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
    images: 'Creando imagenes',
    video: 'Ensamblando video',
    reassemble: 'Re-ensamblando',
    upload: 'Subiendo a YouTube',
  }
  return map[phase] || phase
}

// Glitch text effect component
function GlitchText({ text, interval = 5000 }: { text: string; interval?: number }) {
  const [display, setDisplay] = useState(text)
  const [glitching, setGlitching] = useState(false)
  const original = text

  useEffect(() => {
    const timer = setInterval(() => {
      setGlitching(true)
      // Corrupt the text briefly
      const chars = '!@#$%&*<>?/\\'
      const corrupted = original.split('').map(c =>
        Math.random() > 0.7 ? chars[Math.floor(Math.random() * chars.length)] : c
      ).join('')
      setDisplay(corrupted)
      setTimeout(() => {
        setDisplay(original)
        setGlitching(false)
      }, 150)
    }, interval)
    return () => clearInterval(timer)
  }, [original, interval])

  return (
    <span className={glitching ? 'text-neon-red' : ''} style={{
      textShadow: glitching ? '1px 0 #ff3355, -1px 0 #00e5ff' : 'none',
    }}>
      {display}
    </span>
  )
}

// Single pipeline card
function PipelineCard({ v }: { v: PipelineItem }) {
  const [completed, setCompleted] = useState(false)
  const [shake, setShake] = useState(false)
  const [soundEnabled, setSoundEnabled] = useState(false)
  const prevProgress = useRef(v.progress)
  const { glitchTick } = useEasterEgg()

  // Detect completion
  useEffect(() => {
    if (prevProgress.current != null && prevProgress.current < 100 && v.progress === 100) {
      setCompleted(true)
      setShake(true)
      // Load confetti dynamically
      import('canvas-confetti').then(({ default: confetti }) => {
        confetti({
          particleCount: 80,
          spread: 60,
          origin: { y: 0.5, x: 0.5 },
          colors: ['#ff3355', '#ffb830', '#00e5ff', '#a855f7'],
        })
      }).catch(() => {})
      setTimeout(() => setShake(false), 600)
    }
    prevProgress.current = v.progress
  }, [v.progress])

  const isGenerating = v.status === 'generating'
  const progress = v.progress ?? 0

  return (
    <motion.div
      animate={shake ? { x: [-2, 2, -2, 2, 0] } : {}}
      transition={{ duration: 0.4 }}
      className="relative"
    >
      <Link
        key={`${v.id}-${glitchTick}`}
        to={`/videos/${v.id}/edit`}
        className={`flex items-center gap-3 p-3 rounded-lg transition-all group border
          ${completed
            ? 'bg-neon-gold/5 border-neon-gold/30 shadow-[0_0_15px_rgba(255,184,48,0.1)]'
            : 'bg-dark-700/50 hover:bg-dark-600/50 border-surface-border/30'
          }`}
      >
        {/* Icon */}
        <div className={`shrink-0 w-10 h-10 rounded-lg flex items-center justify-center
          ${completed ? 'bg-neon-gold/20' : 'bg-dark-600'}`}>
          {isGenerating ? (
            <div className="w-5 h-5 border-2 border-neon-gold border-t-transparent rounded-full animate-spin" />
          ) : completed ? (
            <motion.div
              initial={{ scale: 0 }}
              animate={{ scale: 1 }}
              className="w-5 h-5 rounded-full bg-neon-gold flex items-center justify-center"
            >
              <span className="text-dark-900 text-xs">✓</span>
            </motion.div>
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
            <span className={`badge ${statusBadge(v.status)} shrink-0 text-[10px]`}>
              {isGenerating ? (
                <GlitchText text={statusLabel(v.status)} interval={5000} />
              ) : (
                statusLabel(v.status)
              )}
            </span>
          </div>
          <div className="flex items-center gap-2 mt-1">
            <span className="text-xs text-gray-500">{v.channel_name}</span>
            <span className="text-xs text-gray-600">·</span>
            <span className="text-xs text-gray-600">
              {v.progress_phase ? PhaseLabel(v.progress_phase) : formatDate(v.created_at)}
            </span>
          </div>

          {/* Progress bar with shimmer */}
          {isGenerating && progress > 0 && (
            <div className="mt-2 w-full h-1.5 bg-dark-600 rounded-full overflow-hidden relative">
              <motion.div
                className="h-full rounded-full relative overflow-hidden"
                style={{
                  width: `${Math.min(progress, 100)}%`,
                  background: 'linear-gradient(90deg, #ffb830, #ff3355, #a855f7)',
                  backgroundSize: '200% 100%',
                  animation: 'progress-shift 2s linear infinite',
                } as React.CSSProperties}
              >
                {/* Shimmer overlay */}
                <div className="absolute inset-0 progress-shimmer" />
                {/* Digital rain particles in bar */}
                <div className="absolute inset-0 overflow-hidden">
                  {[...Array(3)].map((_, i) => (
                    <div key={i} className="absolute w-0.5 h-1.5 bg-white/30 rounded-full"
                      style={{
                        left: `${20 + i * 30}%`,
                        animation: `digital-rain-bar ${1.5 + i * 0.5}s linear infinite`,
                        animationDelay: `${i * 0.3}s`,
                      }}
                    />
                  ))}
                </div>
              </motion.div>
              <span className="absolute right-1 top-1/2 -translate-y-1/2 text-[8px] text-gray-500 font-mono">
                {Math.round(progress)}%
              </span>
            </div>
          )}

          {/* Completed flash */}
          {completed && !isGenerating && (
            <motion.div
              initial={{ opacity: 1 }}
              animate={{ opacity: 0 }}
              transition={{ duration: 2 }}
              className="text-[10px] text-neon-gold mt-1"
            >
              Completado
            </motion.div>
          )}
        </div>
      </Link>

      {/* Sound toggle (small) */}
      <button
        onClick={(e) => { e.preventDefault(); setSoundEnabled(s => !s) }}
        className="absolute top-1 right-1 p-1 rounded hover:bg-dark-600 text-gray-600 hover:text-gray-400 text-xs"
        title={soundEnabled ? 'Silenciar sonidos' : 'Activar sonidos'}
      >
        {soundEnabled ? <Volume2 size={10} /> : <VolumeX size={10} />}
      </button>
    </motion.div>
  )
}

export default function PipelineSection({ pipeline }: PipelineSectionProps) {
  return (
    <div>
      {pipeline.length === 0 ? (
        <div className="text-center py-8">
          <Zap size={36} className="mx-auto mb-3 text-gray-700" />
          <p className="text-gray-500 text-sm">Sin actividad en este momento</p>
          <p className="text-gray-600 text-xs mt-1 mb-4">
            Todo en calma. Lanzas una nueva generacion?
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
            <PipelineCard key={v.id} v={v} />
          ))}
        </div>
      )}

      <style>{`
        @keyframes progress-shift {
          0% { background-position: 200% 0; }
          100% { background-position: 0 0; }
        }
        @keyframes digital-rain-bar {
          0% { transform: translateY(-100%); opacity: 0; }
          50% { opacity: 1; }
          100% { transform: translateY(600%); opacity: 0; }
        }
      `}</style>
    </div>
  )
}
