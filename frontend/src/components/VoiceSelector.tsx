/** VoiceSelector — browser-based voice picker with audio preview.
 *
 *  Fetches the voice catalog from GET /api/voices and renders voice cards
 *  grouped by TTS engine. Each card shows a ▶ play button that streams the
 *  pre-generated preview clip. Selecting a voice automatically sets the
 *  appropriate config keys (TTS_ENGINE, VOICE_ID / KOKORO_VOICE).
 */
import { useState, useEffect, useRef, useCallback } from 'react'
import { Play, Pause, ChevronDown, ChevronRight } from 'lucide-react'

// ── Types ────────────────────────────────────────────────────

interface Voice {
  key: string           // "kokoro:ef_dora" or "edgetts:es-MX-DaliaNeural"
  name: string
  engine: "kokoro" | "edgetts"
  engine_label: string
  gender: "male" | "female"
  preview_url: string
}

interface GroupedVoices {
  engine: string
  label: string
  voices: Voice[]
}

interface Props {
  config: Record<string, any>
  onUpdateField: (key: string, value: any) => void
}

// ── Engine grouping ──────────────────────────────────────────

const ENGINE_LABELS: Record<string, string> = {
  kokoro: "Kokoro (local · 3 voces)",
  edgetts: "Edge-TTS (cloud · 3 voces)",
}

const GENDER_ICON: Record<string, string> = {
  male: "♂️",
  female: "♀️",
}

// ═══════════════════════════════════════════════════════════════

export default function VoiceSelector({ config, onUpdateField }: Props) {
  const [voices, setVoices] = useState<GroupedVoices[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [playingKey, setPlayingKey] = useState<string | null>(null)
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({})
  const audioRef = useRef<HTMLAudioElement | null>(null)

  // ── Determine currently selected voice ────────────────────
  const ttsEngine = config.TTS_ENGINE || 'edgetts'
  const selectedVoiceKey =
    ttsEngine === 'kokoro'
      ? `kokoro:${config.KOKORO_VOICE || 'em_santa'}`
      : `edgetts:${config.VOICE_ID || 'es-MX-JorgeNeural'}`

  // ── Fetch voice catalog ───────────────────────────────────
  useEffect(() => {
    let cancelled = false
    setLoading(true)
    fetch('api/voices')
      .then(r => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`)
        return r.json()
      })
      .then(data => {
        if (cancelled) return
        const list: Voice[] = data.voices || []
        const groups: GroupedVoices[] = []
        for (const v of list) {
          let grp = groups.find(g => g.engine === v.engine)
          if (!grp) {
            grp = {
              engine: v.engine,
              label: ENGINE_LABELS[v.engine] || v.engine_label,
              voices: [],
            }
            groups.push(grp)
          }
          grp.voices.push(v)
        }
        setVoices(groups)
      })
      .catch(e => {
        if (!cancelled) setError(e.message)
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => { cancelled = true }
  }, [])

  // ── Play / stop preview ───────────────────────────────────
  const togglePlay = useCallback((voice: Voice) => {
    if (playingKey === voice.key) {
      // Stop current
      audioRef.current?.pause()
      audioRef.current = null
      setPlayingKey(null)
      return
    }
    // Stop any existing
    audioRef.current?.pause()
    audioRef.current = null

    const audio = new Audio(voice.preview_url)
    audioRef.current = audio
    audio.onended = () => setPlayingKey(null)
    audio.onerror = () => setPlayingKey(null)
    audio.play().catch(() => setPlayingKey(null))
    setPlayingKey(voice.key)
  }, [playingKey])

  // ── Select a voice ─────────────────────────────────────────
  const selectVoice = useCallback((voice: Voice) => {
    if (voice.engine === 'kokoro') {
      onUpdateField('TTS_ENGINE', 'kokoro')
      onUpdateField('KOKORO_VOICE', voice.key.replace('kokoro:', ''))
      onUpdateField('VOICE_ID', undefined)
    } else {
      onUpdateField('TTS_ENGINE', 'edgetts')
      onUpdateField('VOICE_ID', voice.key.replace('edgetts:', ''))
      onUpdateField('KOKORO_VOICE', undefined)
    }
  }, [onUpdateField])

  // ── Toggle group collapse ──────────────────────────────────
  const toggleGroup = (engine: string) => {
    setCollapsed(prev => ({ ...prev, [engine]: !prev[engine] }))
  }

  // ── Render ─────────────────────────────────────────────────
  if (loading) {
    return (
      <div className="flex items-center gap-2 py-1">
        <div className="animate-spin rounded-full h-3 w-3 border border-neon-cyan border-t-transparent" />
        <span className="text-xs text-gray-500">Cargando voces…</span>
      </div>
    )
  }

  if (error) {
    return <span className="text-xs text-red-400">Error: {error}</span>
  }

  if (voices.length === 0) {
    return <span className="text-xs text-gray-500">No hay voces disponibles</span>
  }

  return (
    <div className="space-y-2 -mt-1">
      {voices.map(group => {
        const isCollapsed = collapsed[group.engine] || false
        return (
          <div key={group.engine}>
            {/* Group header */}
            <button
              onClick={() => toggleGroup(group.engine)}
              className="flex items-center gap-1 text-[11px] text-gray-500 hover:text-gray-400 w-full text-left mb-1"
            >
              {isCollapsed ? <ChevronRight size={10} /> : <ChevronDown size={10} />}
              <span>{group.label}</span>
            </button>

            {!isCollapsed && (
              <div className="space-y-1">
                {group.voices.map(voice => {
                  const isSelected = selectedVoiceKey === voice.key
                  const isPlaying = playingKey === voice.key
                  return (
                    <div
                      key={voice.key}
                      onClick={() => selectVoice(voice)}
                      className={`flex items-center gap-1.5 px-1.5 py-1 rounded cursor-pointer text-xs transition-colors border ${
                        isSelected
                          ? 'bg-neon-cyan/10 border-neon-cyan/40 text-neon-cyan'
                          : 'bg-dark-800 border-transparent text-gray-400 hover:bg-dark-700 hover:text-gray-300'
                      }`}
                    >
                      {/* Play button */}
                      <button
                        onClick={e => { e.stopPropagation(); togglePlay(voice) }}
                        className={`shrink-0 w-5 h-5 flex items-center justify-center rounded-full text-[10px] transition-colors ${
                          isPlaying
                            ? 'bg-neon-gold text-dark-900'
                            : isSelected
                              ? 'bg-neon-cyan/20 text-neon-cyan hover:bg-neon-cyan/40'
                              : 'bg-dark-700 text-gray-500 hover:bg-dark-600 hover:text-gray-300'
                        }`}
                        title={isPlaying ? 'Detener' : 'Escuchar'}
                      >
                        {isPlaying ? <Pause size={10} /> : <Play size={10} className="ml-0.5" />}
                      </button>

                      {/* Voice name & metadata */}
                      <div className="flex-1 min-w-0 flex items-center gap-1">
                        <span className="truncate font-medium text-[11px]">
                          {voice.name} {GENDER_ICON[voice.gender]}
                        </span>
                      </div>

                      {/* Selection indicator */}
                      {isSelected && (
                        <span className="shrink-0 text-[9px] text-neon-cyan font-medium">Activa</span>
                      )}
                    </div>
                  )
                })}
              </div>
            )}
          </div>
        )
      })}
    </div>
  )
}
