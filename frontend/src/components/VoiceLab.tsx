/** VoiceLab — escucha la voz elegida en cada tono + relato largo.
 *
 *  Sintetiza en runtime con el mismo motor/prosodia que producción
 *  (endpoints /api/voices/clip y /api/voices/relato).
 */
import { useState, useEffect, useRef, useCallback } from 'react'
import { Play, Pause, AlertCircle } from 'lucide-react'
import { api } from '../lib/api'

const TONE_LABELS: Record<string, string> = {
  neutro: 'Neutro',
  suspense: 'Suspense',
  misterio: 'Misterio',
  tension: 'Tensión',
  revelacion: 'Revelación',
  asombro: 'Asombro',
  tristeza: 'Tristeza',
  esperanza: 'Esperanza',
  reflexion: 'Reflexión',
  enfasis: 'Énfasis',
  cierre: 'Cierre',
}

const DEFAULT_TONES = Object.keys(TONE_LABELS)

interface Props {
  config: Record<string, any>
  slug?: string
}

export default function VoiceLab({ config, slug }: Props) {
  const engine = config.TTS_ENGINE || 'edgetts'
  const voice =
    engine === 'kokoro'
      ? config.KOKORO_VOICE || 'em_santa'
      : config.VOICE_ID || 'es-MX-JorgeNeural'

  const [tones, setTones] = useState<string[]>(DEFAULT_TONES)
  const [playing, setPlaying] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const audioRef = useRef<HTMLAudioElement | null>(null)

  useEffect(() => {
    let cancelled = false
    api.getVoices()
      .then((d: any) => {
        if (!cancelled && Array.isArray(d?.tones) && d.tones.length) setTones(d.tones)
      })
      .catch(() => { /* fallback to DEFAULT_TONES */ })
    return () => { cancelled = true }
  }, [])

  const stop = useCallback(() => {
    audioRef.current?.pause()
    audioRef.current = null
    setPlaying(null)
  }, [])

  const play = useCallback((key: string, url: string) => {
    if (playing === key) {
      stop()
      return
    }
    audioRef.current?.pause()
    setError(null)
    const audio = new Audio(url)
    audioRef.current = audio
    audio.onended = () => setPlaying(null)
    audio.onerror = () => {
      setPlaying(null)
      setError('No se pudo reproducir la muestra. Comprueba que la API y el motor TTS estén disponibles.')
    }
    audio.play().then(() => setPlaying(key)).catch(() => {
      setPlaying(null)
      setError('El navegador bloqueó la reproducción. Vuelve a pulsar el botón.')
    })
  }, [playing, stop])

  return (
    <div className="mt-3 rounded border border-dark-600 bg-dark-800/50 p-2.5">
      <div className="flex items-center justify-between mb-2">
        <div>
          <div className="text-[11px] font-semibold text-gray-300">🧪 Laboratorio de voz</div>
          <div className="text-[10px] text-gray-500">
            Voz activa: <span className="text-neon-cyan">{voice}</span> · {engine}
          </div>
        </div>
        <button
          onClick={() => play('relato', api.voiceRelatoUrl(engine, voice, slug))}
          className={`text-[10px] px-2 py-1 rounded border transition-colors ${
            playing === 'relato'
              ? 'bg-neon-gold text-dark-900 border-neon-gold'
              : 'bg-dark-700 text-gray-300 border-dark-600 hover:border-neon-gold/60'
          }`}
          title="Relato largo con todos los tonos"
        >
          {playing === 'relato' ? '⏸ Detener relato' : '▶ Relato largo (todos los tonos)'}
        </button>
      </div>

      <div className="grid grid-cols-2 gap-1">
        {tones.map(tone => {
          const isPlaying = playing === `tone:${tone}`
          return (
            <button
              key={tone}
              onClick={() => play(`tone:${tone}`, api.voiceClipUrl(engine, voice, tone, slug))}
              className={`flex items-center gap-1.5 px-2 py-1 rounded text-[11px] border text-left transition-colors ${
                isPlaying
                  ? 'bg-neon-cyan/15 border-neon-cyan/50 text-neon-cyan'
                  : 'bg-dark-700 border-transparent text-gray-400 hover:text-gray-200 hover:border-dark-500'
              }`}
              title={`Probar el tono ${TONE_LABELS[tone] || tone}`}
            >
              {isPlaying
                ? <Pause size={10} className="shrink-0" />
                : <Play size={10} className="shrink-0 ml-0.5" />}
              <span className="truncate">{TONE_LABELS[tone] || tone}</span>
            </button>
          )
        })}
      </div>

      {error && (
        <div className="mt-2 flex items-start gap-1 text-[10px] text-red-400">
          <AlertCircle size={11} className="mt-0.5 shrink-0" />
          <span>{error}</span>
        </div>
      )}
    </div>
  )
}
