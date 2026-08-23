import { useState, useEffect, useCallback } from 'react'
import { api } from '../lib/api'
import { ShieldAlert, ShieldCheck, Loader2, AlertTriangle, Gauge, Database, HardDrive } from 'lucide-react'

interface PacingSummary {
  active_profile: string
  available_profiles: string[]
  pacing: Record<string, any>
  active_channels: { id: number; slug: string; name: string; google_account: string }[]
}

interface FactoryStatus {
  factory_ok: boolean
  disk: { free_mb: number; min_mb: number; ok: boolean }
  credits: Record<string, { status?: string; balance_usd?: number; has_quota?: boolean }>
  credits_ok: boolean
  backlog: {
    awaiting_upload: number
    warming: number
    queued_shorts: number
    total_items: number
    daily_capacity: number
    eta_days: number
  }
  continuous_generation: boolean
}

const PROFILE_META: Record<string, { label: string; color: string; desc: string }> = {
  strike: {
    label: 'Strike',
    color: 'text-red-400 border-red-500/40 bg-red-500/10 hover:bg-red-500/20',
    desc: 'Situacion actual: 1 short/dia, 1 longform/dia, espaciado 45min',
  },
  recovery: {
    label: 'Recovery',
    color: 'text-amber-400 border-amber-500/40 bg-amber-500/10 hover:bg-amber-500/20',
    desc: 'Relajacion gradual: 2 shorts/dia, espaciado 30min',
  },
  normal: {
    label: 'Normal',
    color: 'text-emerald-400 border-emerald-500/40 bg-emerald-500/10 hover:bg-emerald-500/20',
    desc: 'Frecuencia maxima: 3 shorts/dia, 2 longform/dia, espaciado 20min',
  },
}

function PacingRow({ label, value, unit }: { label: string; value: any; unit?: string }) {
  return (
    <div className="flex items-center justify-between text-xs py-1 border-b border-surface-border/40 last:border-0">
      <span className="text-gray-400">{label}</span>
      <span className="text-gray-200 font-medium">
        {value}
        {unit ? <span className="text-gray-500 ml-1">{unit}</span> : null}
      </span>
    </div>
  )
}

export default function PacingProfileCard() {
  const [summary, setSummary] = useState<PacingSummary | null>(null)
  const [factory, setFactory] = useState<FactoryStatus | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [confirmTarget, setConfirmTarget] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const [s, f] = await Promise.all([api.getPacingProfile(), api.getFactoryStatus()])
      setSummary(s)
      setFactory(f)
    } catch (e: any) {
      setError(e?.message || 'No se pudo cargar el perfil de cadencia.')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  const switchProfile = useCallback(async (profile: string) => {
    setSaving(true)
    setError(null)
    try {
      await api.setPacingProfile(profile)
      setConfirmTarget(null)
      await load()
    } catch (e: any) {
      setError(e?.message || 'No se pudo cambiar el perfil.')
    } finally {
      setSaving(false)
    }
  }, [load])

  if (loading) {
    return (
      <section className="glass rounded-xl p-5">
        <div className="flex items-center gap-2 text-gray-400 text-sm">
          <Loader2 size={16} className="animate-spin" /> Cargando perfil de cadencia...
        </div>
      </section>
    )
  }

  const active = summary?.active_profile ?? 'strike'
  const p = summary?.pacing ?? {}

  return (
    <section className="glass rounded-xl p-5 space-y-4">
      <div className="flex items-center justify-between">
        <h3 className="font-display text-base font-semibold text-white flex items-center gap-2">
          <Gauge size={16} className="text-neon-gold" /> Perfil de Cadencia (Strike Mode)
          <span className="text-xs text-gray-500 font-normal">
            — el switch central que relaja/endurece TODAS las reglas de subida
          </span>
        </h3>
        {summary && (
          <span className="text-[10px] text-gray-500">
            {summary.active_channels.length} canal(es) activo(s)
          </span>
        )}
      </div>

      {/* ── Selector de perfil ── */}
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-2">
        {(summary?.available_profiles ?? Object.keys(PROFILE_META)).map((name) => {
          const meta = PROFILE_META[name] ?? { label: name, color: '', desc: '' }
          const isActive = name === active
          return (
            <button
              key={name}
              disabled={saving}
              onClick={() => {
                if (isActive) return
                setConfirmTarget(name)
              }}
              className={`relative rounded-xl border px-4 py-3 text-left transition-all disabled:opacity-50 ${
                isActive ? `${meta.color} ring-1 ring-inset` : 'bg-dark-700/40 border-surface-border hover:bg-dark-700/70 text-gray-300'
              }`}
              aria-pressed={isActive}
            >
              <div className="flex items-center gap-2">
                {name === 'strike' ? <ShieldAlert size={14} /> : <ShieldCheck size={14} />}
                <span className="font-medium text-sm">{meta.label}</span>
                {isActive && <span className="ml-auto text-[10px] uppercase tracking-wide opacity-70">activo</span>}
              </div>
              <p className="text-[10px] mt-1 opacity-80 leading-snug">{meta.desc}</p>
            </button>
          )
        })}
      </div>

      {/* ── Confirmacion inline ── */}
      {confirmTarget && (
        <div className="flex items-center gap-3 bg-amber-500/10 border border-amber-500/30 rounded-lg p-3">
          <AlertTriangle size={16} className="text-amber-400 shrink-0" />
          <p className="text-xs text-amber-200 flex-1">
            Cambiar a perfil <strong>{confirmTarget}</strong>? Fábrica y válvula se reajustan de golpe
            (shorts/día, longform/día, gaps y espaciado).
          </p>
          <button
            onClick={() => switchProfile(confirmTarget)}
            disabled={saving}
            className="px-3 py-1.5 rounded-lg bg-amber-500/20 border border-amber-500/40 text-amber-300 text-xs font-medium hover:bg-amber-500/30 disabled:opacity-50"
          >
            {saving ? 'Aplicando...' : 'Confirmar'}
          </button>
          <button
            onClick={() => setConfirmTarget(null)}
            disabled={saving}
            className="px-3 py-1.5 rounded-lg bg-dark-600 text-gray-400 text-xs hover:bg-dark-500"
          >
            Cancelar
          </button>
        </div>
      )}

      {error && <p className="text-xs text-red-400 bg-red-500/10 border border-red-500/30 rounded-lg p-2">{error}</p>}

      {/* ── Valores resueltos ── */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
        <div className="bg-dark-700/50 rounded-xl p-3">
          <p className="text-[10px] uppercase tracking-wide text-gray-500 mb-1">Shorts</p>
          <PacingRow label="Por canal y dia" value={p.shorts_per_channel_day} unit="shorts" />
          <PacingRow label="Global dia" value={p.shorts_global_day} unit="shorts" />
          <PacingRow label="Cooldown mismo canal" value={p.shorts_cooldown_min} unit="min" />
          <PacingRow label="Gap native↔native" value={p.shorts_same_type_gap_min} unit="min" />
        </div>
        <div className="bg-dark-700/50 rounded-xl p-3">
          <p className="text-[10px] uppercase tracking-wide text-gray-500 mb-1">Long-form</p>
          <PacingRow label="Publicaciones por canal y dia" value={p.max_longform_publish_day} unit="videos" />
          <PacingRow label="Gap publicacion mismo canal" value={p.same_channel_publish_gap_h} unit="h" />
          <PacingRow label="Gap subida mismo canal" value={p.same_channel_upload_gap_h} unit="h" />
        </div>
        <div className="bg-dark-700/50 rounded-xl p-3">
          <p className="text-[10px] uppercase tracking-wide text-gray-500 mb-1">Espaciado global</p>
          <PacingRow label="Entre canales distintos" value={p.global_upload_spacing_min} unit="min" />
          <PacingRow label="Cap por cuenta Google" value={p.account_daily_upload_cap} unit="subidas/dia" />
          <PacingRow label="Filtro content_safety" value={p.content_safety_disabled ? 'desactivado' : 'activo'} />
        </div>
      </div>

      {/* ── Estado de la fábrica (Fase 4) ── */}
      {factory && (
        <div className="border-t border-surface-border/50 pt-3">
          <div className="flex items-center justify-between mb-2">
            <p className="text-[10px] uppercase tracking-wide text-gray-500 flex items-center gap-1.5">
              <Database size={11} /> Estado de la fábrica
            </p>
            <span className={`text-[10px] px-2 py-0.5 rounded-full border ${
              factory.factory_ok
                ? 'text-emerald-400 border-emerald-500/40 bg-emerald-500/10'
                : 'text-red-400 border-red-500/40 bg-red-500/10'
            }`}>
              {factory.factory_ok ? 'Generando' : 'Pausada'}
            </span>
          </div>
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 text-xs">
            <div className="bg-dark-700/40 rounded-lg p-2">
              <p className="text-gray-500 flex items-center gap-1"><HardDrive size={10} /> Disco</p>
              <p className={`font-medium ${factory.disk.ok ? 'text-gray-200' : 'text-red-400'}`}>
                {(factory.disk.free_mb / 1024).toFixed(1)} GB
                <span className="text-gray-500 ml-1">/ {(factory.disk.min_mb / 1024).toFixed(0)}</span>
              </p>
            </div>
            <div className="bg-dark-700/40 rounded-lg p-2">
              <p className="text-gray-500">Credits LLM</p>
              <p className={`font-medium ${factory.credits_ok ? 'text-gray-200' : 'text-red-400'}`}>
                {factory.credits_ok ? 'OK' : 'Sin crédito'}
              </p>
            </div>
            <div className="bg-dark-700/40 rounded-lg p-2">
              <p className="text-gray-500">En cola</p>
              <p className="font-medium text-gray-200">{factory.backlog.total_items} <span className="text-gray-500 text-[10px]">items</span></p>
              <p className="text-[10px] text-gray-500">+{factory.backlog.queued_shorts} shorts</p>
            </div>
            <div className="bg-dark-700/40 rounded-lg p-2">
              <p className="text-gray-500">ETA drenaje</p>
              <p className="font-medium text-neon-cyan">{factory.backlog.eta_days ?? '—'} <span className="text-gray-500 text-[10px]">días</span></p>
              <p className="text-[10px] text-gray-500">cap {factory.backlog.daily_capacity}/día</p>
            </div>
          </div>
          <p className="text-[10px] text-gray-600 mt-2">
            Fábrica continua: {factory.continuous_generation ? 'activa (genera 24/7)' : 'inactiva (ventana 36h)'}
          </p>
        </div>
      )}
    </section>
  )
}
