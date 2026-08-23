/**
 * SocialDistributionTab — identidad y credenciales por red + rendimiento.
 *
 * Sustituye al modal "Redes Sociales": se muestra como pestaña completa en el
 * panel del canal. Guarda para cada plataforma:
 *   - username (handle / usuario)
 *   - password / token (API key, app password, page token, JSON client…)
 *   - account_email (correo de registro) + account_email_password (correo)
 *   - account_password (contraseña de login de la plataforma)
 *   - notes (recuperación, teléfono, etc.)
 *
 * Los secretos NUNCA salen en el listado: la API devuelve solo flags
 * (has_api_key, has_email_password, has_account_password) y se revelan
 * bajo demanda con el endpoint /reveal.
 */
import { useEffect, useState } from 'react'
import { api } from '../lib/api'
import { SOCIAL_PLATFORMS, type SocialAccount } from '../types/channel'

interface Props {
  channelId: number
}

interface AccountForm {
  platform: string
  username: string
  password: string
  account_email: string
  account_email_password: string
  account_password: string
  notes: string
  enabled: boolean
}

interface Revealed {
  platform: string
  field: string
  value: string
}

const EMPTY_FORM: AccountForm = {
  platform: '', username: '', password: '',
  account_email: '', account_email_password: '', account_password: '',
  notes: '', enabled: true,
}

// Plataformas cuya credencial es un TOKEN/KEY (no contraseña de navegador)
const TOKEN_PLATFORMS = ['rumble', 'facebook', 'dailymotion', 'bluesky', 'mastodon']

function fmt(n: number | undefined | null): string {
  const v = Number(n || 0)
  if (v >= 1_000_000) return (v / 1_000_000).toFixed(1) + 'M'
  if (v >= 1_000) return (v / 1_000).toFixed(1) + 'k'
  return String(v)
}

export default function SocialDistributionTab({ channelId }: Props) {
  const [accounts, setAccounts] = useState<SocialAccount[]>([])
  const [loading, setLoading] = useState(true)
  const [editingPlatform, setEditingPlatform] = useState<string | null>(null)
  const [form, setForm] = useState<AccountForm>(EMPTY_FORM)
  const [saving, setSaving] = useState<Record<string, boolean>>({})
  const [testing, setTesting] = useState<Record<string, boolean>>({})
  const [revealed, setRevealed] = useState<Revealed | null>(null)
  const [result, setResult] = useState<{ ok: boolean; message: string } | null>(null)

  // Stats por red
  const [stats, setStats] = useState<any[]>([])
  const [collecting, setCollecting] = useState(false)

  useEffect(() => {
    loadAccounts()
    loadStats()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [channelId])

  async function loadAccounts() {
    setLoading(true)
    try {
      const data = await api.getSocialAccounts(channelId)
      setAccounts(Array.isArray(data) ? data : [])
    } catch (e: any) {
      setResult({ ok: false, message: `Error cargando cuentas: ${e.message}` })
    }
    setLoading(false)
  }

  async function loadStats() {
    try {
      const data = await api.getChannelSocialStats(channelId)
      setStats(Array.isArray(data?.per_platform) ? data.per_platform : [])
    } catch (e: any) {
      console.error('Failed to load social stats:', e)
    }
  }

  function startEdit(platform: string) {
    const existing = accounts.find(a => a.platform === platform)
    setForm({
      platform,
      username: existing?.username || '',
      password: '',
      account_email: existing?.account_email || '',
      account_email_password: '',
      account_password: '',
      notes: existing?.notes || '',
      enabled: existing?.enabled ?? true,
    })
    setRevealed(null)
    setEditingPlatform(platform)
  }

  function cancelEdit() {
    setEditingPlatform(null)
    setRevealed(null)
    setForm(EMPTY_FORM)
  }

  async function handleSave() {
    if (!form.platform || !form.username) {
      setResult({ ok: false, message: 'Username es obligatorio' })
      return
    }
    setSaving(prev => ({ ...prev, [form.platform]: true }))
    setResult(null)
    try {
      const existing = accounts.find(a => a.platform === form.platform)
      if (existing) {
        await api.updateSocialAccount(channelId, form.platform, {
          username: form.username,
          password: form.password || undefined,
          enabled: form.enabled,
          account_email: form.account_email || undefined,
          account_email_password: form.account_email_password || undefined,
          account_password: form.account_password || undefined,
          notes: form.notes || undefined,
        })
      } else {
        await api.saveSocialAccount(channelId, form.platform, {
          username: form.username,
          password: form.password,
          enabled: form.enabled,
          account_email: form.account_email || undefined,
          account_email_password: form.account_email_password || undefined,
          account_password: form.account_password || undefined,
          notes: form.notes || undefined,
        })
      }
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
    if (!confirm(`¿Eliminar la cuenta de ${platform} y sus credenciales?`)) return
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
      const res = await api.testSocialLogin(channelId, platform)
      setResult({ ok: !!res?.ok, message: res?.message || (res?.ok ? 'OK' : 'Falló') })
      await loadAccounts()
    } catch (e: any) {
      setResult({ ok: false, message: `Test falló: ${e.message}` })
    }
    setTesting(prev => ({ ...prev, [platform]: false }))
  }

  async function handleReveal(platform: string, field: string) {
    setResult(null)
    try {
      const res = await api.revealSocialCredential(channelId, platform, field)
      setRevealed({ platform, field, value: res?.value || '' })
    } catch (e: any) {
      setResult({ ok: false, message: e.message })
    }
  }

  async function handleCollect() {
    setCollecting(true)
    setResult(null)
    try {
      const res = await api.collectSocialStats(channelId)
      const parts = (res?.results && Object.keys(res.results).length)
        ? Object.entries(res.results).map(([p, s]: any) => `${p}: ${s.updated}/${s.checked}`).join(' · ')
        : 'sin vídeos publicados'
      setResult({ ok: true, message: `Stats sociales recolectadas — ${parts}` })
      await loadStats()
    } catch (e: any) {
      setResult({ ok: false, message: `Error recolectando stats: ${e.message}` })
    }
    setCollecting(false)
  }

  const hasAnyAccount = accounts.length > 0

  return (
    <div className="space-y-5 animate-fade-in">
      {/* ── Identidad y credenciales ─────────────────────────────── */}
      <div className="glass rounded-xl p-5">
        <div className="flex items-center justify-between mb-4">
          <h3 className="font-display text-lg font-semibold text-white flex items-center gap-2">
            🔑 Identidad y credenciales
          </h3>
          <span className="text-[10px] text-gray-500">
            Los secretos se guardan cifrados (Fernet) y se revelan solo bajo demanda
          </span>
        </div>

        {result && (
          <div className={`mb-3 px-3 py-2 rounded-lg text-xs ${result.ok ? 'bg-green-900/40 text-green-300 border border-green-700/40' : 'bg-red-900/40 text-red-300 border border-red-700/40'}`}>
            {result.message}
          </div>
        )}

        {loading ? (
          <div className="text-xs text-gray-500">Cargando cuentas...</div>
        ) : (
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
                    <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
                      <div className="sm:col-span-2">
                        <label className="text-[10px] text-gray-500 block mb-0.5">Usuario / Handle</label>
                        <input type="text" value={form.username}
                          onChange={e => setForm(p => ({ ...p, username: e.target.value }))}
                          className="bg-dark-900 border border-surface-border text-white text-xs rounded px-2 py-1.5 w-full" />
                      </div>
                      <div className="sm:col-span-2">
                        <label className="text-[10px] text-gray-500 block mb-0.5">
                          {TOKEN_PLATFORMS.includes(platform.id) ? 'API Key / Token' : 'Contraseña'}
                          {acct?.has_api_key && <span className="text-green-400 ml-1">(guardada)</span>}
                        </label>
                        <div className="flex gap-1.5">
                          <input type="password" placeholder="Dejar en blanco = no cambiar" value={form.password}
                            onChange={e => setForm(p => ({ ...p, password: e.target.value }))}
                            className="bg-dark-900 border border-surface-border text-white text-xs rounded px-2 py-1.5 w-full" />
                          {acct?.has_api_key && (
                            <button onClick={() => handleReveal(platform.id, 'api_key')}
                              className="px-2 py-1 bg-dark-600 text-neon-cyan rounded text-[10px] hover:bg-dark-500 shrink-0">👁</button>
                          )}
                        </div>
                      </div>
                      <div>
                        <label className="text-[10px] text-gray-500 block mb-0.5">Correo de registro</label>
                        <input type="text" placeholder="sincronias.redes@gmx.com" value={form.account_email}
                          onChange={e => setForm(p => ({ ...p, account_email: e.target.value }))}
                          className="bg-dark-900 border border-surface-border text-white text-xs rounded px-2 py-1.5 w-full" />
                      </div>
                      <div>
                        <label className="text-[10px] text-gray-500 block mb-0.5">
                          Contraseña del correo {acct?.has_email_password && <span className="text-green-400">(guardada)</span>}
                        </label>
                        <div className="flex gap-1.5">
                          <input type="password" placeholder="Dejar en blanco = no cambiar" value={form.account_email_password}
                            onChange={e => setForm(p => ({ ...p, account_email_password: e.target.value }))}
                            className="bg-dark-900 border border-surface-border text-white text-xs rounded px-2 py-1.5 w-full" />
                          {acct?.has_email_password && (
                            <button onClick={() => handleReveal(platform.id, 'email_password')}
                              className="px-2 py-1 bg-dark-600 text-neon-cyan rounded text-[10px] hover:bg-dark-500 shrink-0">👁</button>
                          )}
                        </div>
                      </div>
                      <div>
                        <label className="text-[10px] text-gray-500 block mb-0.5">
                          Contraseña de login (plataforma) {acct?.has_account_password && <span className="text-green-400">(guardada)</span>}
                        </label>
                        <div className="flex gap-1.5">
                          <input type="password" placeholder="Dejar en blanco = no cambiar" value={form.account_password}
                            onChange={e => setForm(p => ({ ...p, account_password: e.target.value }))}
                            className="bg-dark-900 border border-surface-border text-white text-xs rounded px-2 py-1.5 w-full" />
                          {acct?.has_account_password && (
                            <button onClick={() => handleReveal(platform.id, 'account_password')}
                              className="px-2 py-1 bg-dark-600 text-neon-cyan rounded text-[10px] hover:bg-dark-500 shrink-0">👁</button>
                          )}
                        </div>
                      </div>
                      <div>
                        <label className="text-[10px] text-gray-500 block mb-0.5">Notas (recuperación, teléfono…)</label>
                        <input type="text" value={form.notes}
                          onChange={e => setForm(p => ({ ...p, notes: e.target.value }))}
                          className="bg-dark-900 border border-surface-border text-white text-xs rounded px-2 py-1.5 w-full" />
                      </div>
                      <div className="flex items-end gap-2 pb-0.5">
                        <label className="flex items-center gap-1.5 text-xs text-gray-400">
                          <input type="checkbox" checked={form.enabled}
                            onChange={e => setForm(p => ({ ...p, enabled: e.target.checked }))}
                            className="accent-neon-cyan" /> Habilitada
                        </label>
                      </div>
                    </div>
                    {revealed && revealed.platform === platform.id && (
                      <div className="bg-dark-900/60 rounded border border-neon-gold/30 px-2 py-1.5 text-xs flex items-center justify-between gap-2">
                        <span className="text-gray-500 shrink-0">
                          {revealed.field === 'api_key' ? 'API Key' : revealed.field === 'email_password' ? 'Correo' : 'Login'}:
                        </span>
                        <span className="text-neon-gold font-mono break-all">{revealed.value}</span>
                      </div>
                    )}
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
                        <div className="text-[10px] text-gray-400 truncate block max-w-[300px]">
                          <span className="text-gray-300">{acct.username}</span>
                          {acct.account_email && <span> · 📧 {acct.account_email}</span>}
                          {acct.notes && <span> · 📝 {acct.notes}</span>}
                          {acct.last_error && <span> · ⚠️ {acct.last_error}</span>}
                        </div>
                      ) : (
                        <span className="text-[10px] text-gray-600">No configurado</span>
                      )}
                    </div>
                  </div>

                  <div className="flex items-center gap-1 shrink-0">
                    {acct ? (
                      <>
                        <button onClick={() => handleToggleEnabled(platform.id, !acct.enabled)}
                          className={`px-2 py-0.5 rounded text-[10px] font-medium transition-colors ${
                            acct.enabled ? 'bg-green-900/30 text-green-400 hover:bg-green-900/50' : 'bg-gray-800 text-gray-500 hover:bg-gray-700'
                          }`}
                          title={acct.enabled ? 'Desactivar' : 'Activar'}>
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
        )}
      </div>

      {/* ── Rendimiento por red ──────────────────────────────────── */}
      <div className="glass rounded-xl p-5">
        <div className="flex items-center justify-between mb-3">
          <h3 className="font-display text-lg font-semibold text-white flex items-center gap-2">
            📊 Rendimiento por red
          </h3>
          <button onClick={handleCollect} disabled={collecting}
            className="px-3 py-1.5 bg-neon-cyan/10 border border-neon-cyan/30 text-neon-cyan rounded text-xs hover:bg-neon-cyan/20 disabled:opacity-50 flex items-center gap-1.5">
            <span>{collecting ? 'Recolectando...' : 'Recolectar stats'}</span>
          </button>
        </div>

        {stats.length === 0 ? (
          <p className="text-xs text-gray-500">
            Aún sin datos. Pulsa "Recolectar stats" cuando haya vídeos publicados en las redes.
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="text-left text-gray-500 border-b border-surface-border/50">
                  <th className="py-2 pr-3">Red</th>
                  <th className="py-2 pr-3 text-right">Publicados</th>
                  <th className="py-2 pr-3 text-right">Vistas</th>
                  <th className="py-2 pr-3 text-right">Likes</th>
                  <th className="py-2 pr-3 text-right">Comentarios</th>
                  <th className="py-2 pr-3 text-right">Reposts</th>
                </tr>
              </thead>
              <tbody>
                {stats.map(s => {
                  const meta = SOCIAL_PLATFORMS.find(p => p.id === s.platform)
                  return (
                    <tr key={s.platform} className="border-b border-surface-border/50 last:border-0">
                      <td className="py-2 pr-3 text-white">{meta?.icon} {meta?.label || s.platform}</td>
                      <td className="py-2 pr-3 text-right text-gray-400">{s.total_published}</td>
                      <td className="py-2 pr-3 text-right text-white">{fmt(s.total_views)}</td>
                      <td className="py-2 pr-3 text-right text-white">{fmt(s.total_likes)}</td>
                      <td className="py-2 pr-3 text-right text-white">{fmt(s.total_comments)}</td>
                      <td className="py-2 pr-3 text-right text-white">{fmt(s.total_reposts)}</td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* ── Estado de la redistribución (enlace) ─────────────────── */}
      {hasAnyAccount && (
        <p className="text-xs text-gray-500">
          ➜ Gestiona el backfill del catálogo en la página <b className="text-neon-cyan">Distribución</b>.
        </p>
      )}
    </div>
  )
}
