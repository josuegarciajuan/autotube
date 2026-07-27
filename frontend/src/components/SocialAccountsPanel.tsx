/** Social media accounts management panel for channel configuration. */
import { useState, useEffect } from 'react'
import { api } from '../lib/api'
import { SOCIAL_PLATFORMS, type SocialAccount } from '../types/channel'

interface Props {
  channelId: number
}

interface AccountForm {
  platform: string
  username: string
  password: string
  enabled: boolean
}

const DEFAULT_TIMING: Record<string, number> = {
  tiktok: 30,
  twitter: 60,
  instagram: 120,
  facebook: 180,
  reddit: 240,
}

export default function SocialAccountsPanel({ channelId }: Props) {
  const [accounts, setAccounts] = useState<SocialAccount[]>([])
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState<Record<string, boolean>>({})
  const [testing, setTesting] = useState<Record<string, boolean>>({})
  const [editingPlatform, setEditingPlatform] = useState<string | null>(null)
  const [form, setForm] = useState<AccountForm>({ platform: '', username: '', password: '', enabled: true })
  const [showPassword, setShowPassword] = useState<Record<string, boolean>>({})
  const [timing, setTiming] = useState<Record<string, number>>(DEFAULT_TIMING)
  const [showTiming, setShowTiming] = useState(false)
  const [showStrategy, setShowStrategy] = useState(false)
  const [result, setResult] = useState<{ ok: boolean; message: string } | null>(null)

  useEffect(() => {
    loadAccounts()
    loadTiming()
  }, [channelId])

  async function loadAccounts() {
    setLoading(true)
    try {
      const data = await api.getSocialAccounts(channelId)
      setAccounts(Array.isArray(data) ? data : [])
    } catch (e: any) {
      console.error('Failed to load social accounts:', e)
    }
    setLoading(false)
  }

  async function loadTiming() {
    try {
      const data = await api.getSocialTiming(channelId)
      if (data && typeof data === 'object') {
        setTiming({ ...DEFAULT_TIMING, ...data })
      }
    } catch (e: any) { /* defaults are fine */ }
  }

  function startEdit(platform: string) {
    const existing = accounts.find(a => a.platform === platform)
    setForm({
      platform,
      username: existing?.username || '',
      password: '',
      enabled: existing?.enabled ?? true,
    })
    setEditingPlatform(platform)
  }

  function cancelEdit() {
    setEditingPlatform(null)
    setForm({ platform: '', username: '', password: '', enabled: true })
  }

  async function handleSave() {
    if (!form.platform || !form.username) {
      setResult({ ok: false, message: 'Username es obligatorio' })
      return
    }
    setSaving(prev => ({ ...prev, [form.platform]: true }))
    setResult(null)
    try {
      await api.saveSocialAccount(channelId, form.platform, {
        username: form.username,
        password: form.password,
        enabled: form.enabled,
      })
      setResult({ ok: true, message: `Cuenta de ${form.platform} guardada` })
      cancelEdit()
      await loadAccounts()
    } catch (e: any) {
      setResult({ ok: false, message: e.message })
    }
    setSaving(prev => ({ ...prev, [form.platform]: false }))
  }

  async function handleToggleEnabled(platform: string, enabled: boolean) {
    try {
      await api.updateSocialAccount(channelId, platform, { enabled })
      await loadAccounts()
    } catch (e: any) {
      setResult({ ok: false, message: e.message })
    }
  }

  async function handleDelete(platform: string) {
    if (!confirm(`¿Eliminar la cuenta de ${platform}?`)) return
    try {
      await api.deleteSocialAccount(channelId, platform)
      setResult({ ok: true, message: `Cuenta de ${platform} eliminada` })
      await loadAccounts()
    } catch (e: any) {
      setResult({ ok: false, message: e.message })
    }
  }

  async function handleTest(platform: string) {
    setTesting(prev => ({ ...prev, [platform]: true }))
    setResult(null)
    try {
      await api.testSocialLogin(channelId, platform)
      setResult({ ok: true, message: `Login en ${platform}: OK` })
      await loadAccounts()
    } catch (e: any) {
      setResult({ ok: false, message: `Login en ${platform} falló: ${e.message}` })
    }
    setTesting(prev => ({ ...prev, [platform]: false }))
  }

  async function handleSaveTiming() {
    try {
      await api.updateSocialTiming(channelId, timing)
      setResult({ ok: true, message: 'Timing guardado' })
    } catch (e: any) {
      setResult({ ok: false, message: e.message })
    }
  }

  const hasAnyAccount = accounts.length > 0

  return (
    <div className="glass rounded-xl p-5 mt-6 space-y-4 animate-fade-in">
      <div className="flex items-center justify-between">
        <h3 className="font-display text-lg font-semibold text-white flex items-center gap-2">
          🌐 Redes Sociales
        </h3>
        <button
          onClick={() => setShowStrategy(!showStrategy)}
          className="text-[10px] text-neon-cyan/70 hover:text-neon-cyan flex items-center gap-1"
        >
          {showStrategy ? '▼' : '▶'} Estrategia de publicación
        </button>
      </div>

      {showStrategy && (
        <div className="bg-dark-700/30 rounded-lg p-3 text-xs space-y-2 border border-neon-cyan/10">
          <p className="text-gray-300 font-medium mb-1">📋 Estrategia por plataforma:</p>
          {SOCIAL_PLATFORMS.map(p => (
            <div key={p.id} className="flex items-start gap-2 pl-1">
              <span className="text-sm shrink-0 mt-0.5">{p.icon}</span>
              <div>
                <span className="text-white font-medium">{p.label}</span>
                <span className="text-gray-500"> — {p.description}</span>
                <br />
                <span className="text-[10px] text-gray-500">{p.strategy}</span>
              </div>
            </div>
          ))}
        </div>
      )}

      {result && (
        <div className={`px-3 py-2 rounded-lg text-xs ${result.ok ? 'bg-green-900/40 text-green-300 border border-green-700/40' : 'bg-red-900/40 text-red-300 border border-red-700/40'}`}>
          {result.message}
        </div>
      )}

      {loading ? (
        <div className="text-xs text-gray-500">Cargando cuentas...</div>
      ) : (
        <>
          {/* Platform accounts list */}
          <div className="space-y-2">
            {SOCIAL_PLATFORMS.map(platform => {
              const acct = accounts.find(a => a.platform === platform.id)
              const isEditing = editingPlatform === platform.id

              if (isEditing) {
                return (
                  <div key={platform.id} className="bg-dark-700/50 rounded-lg p-3 border border-neon-cyan/30 space-y-2">
                    <div className="flex items-center gap-2 text-sm">
                      <span>{platform.icon}</span>
                      <span className="text-white font-medium">{platform.label}</span>
                    </div>
                    <input
                      type="text" placeholder={`Usuario de ${platform.label}`} value={form.username}
                      onChange={e => setForm(prev => ({ ...prev, username: e.target.value }))}
                      className="bg-dark-900 border border-surface-border text-white text-xs rounded px-2 py-1.5 w-full"
                    />
                    <input
                      type="password" placeholder="Contraseña" value={form.password}
                      onChange={e => setForm(prev => ({ ...prev, password: e.target.value }))}
                      className="bg-dark-900 border border-surface-border text-white text-xs rounded px-2 py-1.5 w-full"
                    />
                    <div className="flex gap-2">
                      <button onClick={handleSave} disabled={saving[platform.id]}
                        className="px-3 py-1 bg-neon-gold text-dark-900 rounded text-xs font-bold hover:bg-neon-gold/80 disabled:opacity-50">
                        {saving[platform.id] ? '...' : 'Guardar'}
                      </button>
                      <button onClick={cancelEdit}
                        className="px-3 py-1 bg-dark-600 text-gray-300 rounded text-xs hover:bg-dark-500">Cancelar</button>
                    </div>
                  </div>
                )
              }

              return (
                <div key={platform.id}
                  className="bg-dark-700/50 rounded-lg p-3 border border-surface-border flex items-center justify-between">
                  <div className="flex items-center gap-3 min-w-0">
                    <span className="text-lg">{platform.icon}</span>
                    <div className="min-w-0">
                      <div className="flex items-center gap-2">
                        <span className="text-white text-xs font-medium">{platform.label}</span>
                        {acct?.enabled ? (
                          <span className="w-1.5 h-1.5 rounded-full bg-green-400" title="Conectado" />
                        ) : acct ? (
                          <span className="w-1.5 h-1.5 rounded-full bg-gray-500" title="Desactivado" />
                        ) : (
                          <span className="w-1.5 h-1.5 rounded-full bg-gray-700" />
                        )}
                      </div>
                      {acct ? (
                        <span className="text-[10px] text-gray-400 truncate block">
                          {acct.username}
                          {acct.has_cookies && ' · sesión activa'}
                          {acct.last_error && ' · ⚠️ error'}
                        </span>
                      ) : (
                        <span className="text-[10px] text-gray-600">No configurado</span>
                      )}
                    </div>
                  </div>

                  <div className="flex items-center gap-1 shrink-0">
                    {acct ? (
                      <>
                        <button
                          onClick={() => handleToggleEnabled(platform.id, !acct.enabled)}
                          className={`px-2 py-0.5 rounded text-[10px] font-medium transition-colors ${
                            acct.enabled ? 'bg-green-900/30 text-green-400 hover:bg-green-900/50' : 'bg-gray-800 text-gray-500 hover:bg-gray-700'
                          }`}
                          title={acct.enabled ? 'Desactivar' : 'Activar'}
                        >
                          {acct.enabled ? 'ON' : 'OFF'}
                        </button>
                        <button onClick={() => startEdit(platform.id)}
                          className="px-2 py-0.5 bg-dark-600 text-gray-300 rounded text-[10px] hover:bg-dark-500">
                          Editar
                        </button>
                        <button onClick={() => handleTest(platform.id)} disabled={testing[platform.id]}
                          className="px-2 py-0.5 bg-neon-cyan/10 text-neon-cyan rounded text-[10px] hover:bg-neon-cyan/20 disabled:opacity-50">
                          {testing[platform.id] ? '...' : 'Test'}
                        </button>
                        <button onClick={() => handleDelete(platform.id)}
                          className="px-2 py-0.5 text-red-400/60 hover:text-red-400 text-[10px]">
                          ✕
                        </button>
                      </>
                    ) : (
                      <button onClick={() => startEdit(platform.id)}
                        className="px-2 py-0.5 bg-neon-gold/10 border border-neon-gold/30 text-neon-gold rounded text-[10px] hover:bg-neon-gold/20">
                        + Conectar
                      </button>
                    )}
                  </div>
                </div>
              )
            })}
          </div>

          {/* Timing configuration */}
          {hasAnyAccount && (
            <div className="mt-3">
              <button
                onClick={() => setShowTiming(!showTiming)}
                className="flex items-center gap-1 text-xs text-neon-cyan hover:text-neon-cyan/80"
              >
                {showTiming ? '▼' : '▶'} Timing de publicación (minutos tras hacer público)
              </button>
              {showTiming && (
                <div className="mt-2 space-y-2 bg-dark-700/30 rounded-lg p-3">
                  {SOCIAL_PLATFORMS.map(platform => {
                    const acct = accounts.find(a => a.platform === platform.id && a.enabled)
                    if (!acct) return null
                    return (
                      <div key={platform.id} className="flex items-center justify-between gap-2">
                        <span className="text-xs text-gray-400 flex items-center gap-1">
                          {platform.icon} {platform.label}
                        </span>
                        <div className="flex items-center gap-1">
                          <span className="text-xs text-gray-500">T+</span>
                          <input
                            type="number" min="0" max="1440"
                            value={timing[platform.id] ?? DEFAULT_TIMING[platform.id]}
                            onChange={e => setTiming(prev => ({ ...prev, [platform.id]: parseInt(e.target.value) || 0 }))}
                            className="bg-dark-900 border border-surface-border text-white text-xs rounded px-1 py-0.5 w-16 text-center"
                          />
                          <span className="text-xs text-gray-500">min</span>
                        </div>
                      </div>
                    )
                  })}
                  <button onClick={handleSaveTiming}
                    className="px-3 py-1 bg-neon-gold/10 border border-neon-gold/30 text-neon-gold rounded text-xs hover:bg-neon-gold/20 mt-1">
                    Guardar Timing
                  </button>
                </div>
              )}
            </div>
          )}
        </>
      )}
    </div>
  )
}
