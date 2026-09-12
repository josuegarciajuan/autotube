/** ProsodyEditor — edita la prosodia de cada tono de narración.
 *
 *  Escribe PROSODY_PROFILES en config_json. La narración expresiva del
 *  pipeline lee estos valores por tono (rate/pitch/volume/pausa).
 */
import type { ProsodyProfile } from '../types/channel'

const TONE_ORDER = [
  'neutro', 'suspense', 'misterio', 'tension', 'revelacion',
  'asombro', 'tristeza', 'esperanza', 'reflexion', 'enfasis', 'cierre',
]

const TONE_LABELS: Record<string, string> = {
  neutro: 'Neutro', suspense: 'Suspense', misterio: 'Misterio',
  tension: 'Tensión', revelacion: 'Revelación', asombro: 'Asombro',
  tristeza: 'Tristeza', esperanza: 'Esperanza', reflexion: 'Reflexión',
  enfasis: 'Énfasis', cierre: 'Cierre',
}

/** Mirror of config/defaults.py PROSODY_PROFILES (used until a channel syncs). */
export const DEFAULT_PROFILES: Record<string, ProsodyProfile> = {
  neutro: { rate: '+0%', pitch: '+0Hz', volume: '+0%', pause_after_ms: 250 },
  suspense: { rate: '-18%', pitch: '-3Hz', volume: '+0%', pause_after_ms: 600 },
  misterio: { rate: '-14%', pitch: '-2Hz', volume: '+0%', pause_after_ms: 500 },
  tension: { rate: '-10%', pitch: '-1Hz', volume: '+2%', pause_after_ms: 300 },
  revelacion: { rate: '+8%', pitch: '+2Hz', volume: '+3%', pause_after_ms: 250 },
  asombro: { rate: '+4%', pitch: '+4Hz', volume: '+2%', pause_after_ms: 350 },
  tristeza: { rate: '-16%', pitch: '-4Hz', volume: '-2%', pause_after_ms: 700 },
  esperanza: { rate: '-4%', pitch: '+1Hz', volume: '+0%', pause_after_ms: 400 },
  reflexion: { rate: '-12%', pitch: '-1Hz', volume: '+0%', pause_after_ms: 600 },
  enfasis: { rate: '-6%', pitch: '+1Hz', volume: '+4%', pause_after_ms: 300 },
  cierre: { rate: '-8%', pitch: '+0Hz', volume: '+0%', pause_after_ms: 500 },
}

interface Props {
  config: Record<string, any>
  onUpdateField: (key: string, value: any) => void
}

export default function ProsodyEditor({ config, onUpdateField }: Props) {
  const raw = config.PROSODY_PROFILES
  const profiles: Record<string, ProsodyProfile> =
    raw && typeof raw === 'object' && !Array.isArray(raw) ? raw : DEFAULT_PROFILES

  const update = (tone: string, field: keyof ProsodyProfile, value: string | number) => {
    const next: Record<string, ProsodyProfile> = {}
    for (const t of TONE_ORDER) {
      next[t] = { ...DEFAULT_PROFILES[t], ...(profiles[t] || {}) }
    }
    next[tone] = { ...next[tone], [field]: value }
    onUpdateField('PROSODY_PROFILES', next)
  }

  const inputCls =
    'w-full bg-dark-900 border border-dark-600 rounded px-1.5 py-0.5 text-[11px] text-gray-200 focus:border-neon-cyan/60 focus:outline-none'

  return (
    <div className="space-y-1.5">
      <div className="flex items-center justify-between">
        <span className="text-[10px] text-gray-500">
          rate: <code>-18%</code> más lento · pitch: <code>-3Hz</code> más grave · pausa tras el tono
        </span>
        <button
          onClick={() => onUpdateField('PROSODY_PROFILES', { ...DEFAULT_PROFILES })}
          className="text-[10px] px-1.5 py-0.5 rounded border border-dark-600 text-gray-400 hover:text-gray-200 hover:border-dark-500"
          title="Restaurar los valores por defecto"
        >
          Restaurar
        </button>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-[10px] border-collapse">
          <thead>
            <tr className="text-gray-500 text-left">
              <th className="py-1 pr-2 font-medium">Tono</th>
              <th className="py-1 pr-1 font-medium">Velocidad</th>
              <th className="py-1 pr-1 font-medium">Pitch</th>
              <th className="py-1 pr-1 font-medium">Volumen</th>
              <th className="py-1 font-medium">Pausa (ms)</th>
            </tr>
          </thead>
          <tbody>
            {TONE_ORDER.map(tone => {
              const p: ProsodyProfile = { ...DEFAULT_PROFILES[tone], ...(profiles[tone] || {}) }
              return (
                <tr key={tone} className="border-t border-dark-700/60">
                  <td className="py-0.5 pr-2 text-gray-300 whitespace-nowrap">
                    {TONE_LABELS[tone] || tone}
                  </td>
                  <td className="py-0.5 pr-1">
                    <input
                      className={inputCls}
                      defaultValue={p.rate}
                      onBlur={e => update(tone, 'rate', e.target.value)}
                    />
                  </td>
                  <td className="py-0.5 pr-1">
                    <input
                      className={inputCls}
                      defaultValue={p.pitch}
                      onBlur={e => update(tone, 'pitch', e.target.value)}
                    />
                  </td>
                  <td className="py-0.5 pr-1">
                    <input
                      className={inputCls}
                      defaultValue={p.volume}
                      onBlur={e => update(tone, 'volume', e.target.value)}
                    />
                  </td>
                  <td className="py-0.5">
                    <input
                      type="number"
                      className={inputCls}
                      defaultValue={p.pause_after_ms}
                      onBlur={e => update(tone, 'pause_after_ms', Number(e.target.value) || 0)}
                    />
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}
