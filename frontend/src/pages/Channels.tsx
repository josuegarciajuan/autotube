import { useState, useEffect } from 'react'
import { Link } from 'react-router-dom'
import { api, formatDate, formatShortNumber } from '../lib/api'
import { Plus, Edit2, Trash2, Radio, Film, Calendar, Youtube, ExternalLink, Eye, Users, Zap, Video } from 'lucide-react'

export default function Channels() {
  const [channels, setChannels] = useState<any[]>([])
  const [channelStats, setChannelStats] = useState<Record<number, any>>({})
  const [loading, setLoading] = useState(true)
  const [showForm, setShowForm] = useState(false)
  const [editing, setEditing] = useState<any>(null)
  const [form, setForm] = useState({ name: '', slug: '', youtube_handle: '', google_account: '' })
  const [error, setError] = useState('')
  const [spamBlocks, setSpamBlocks] = useState<any[]>([])

  useEffect(() => { loadChannels(); loadSpamBlocks() }, [])

  async function loadSpamBlocks() {
    try {
      const res = await api.getSpamBlocks()
      if (res?.ok && Array.isArray(res.channels)) {
        // Mostrar canales bloqueados Y los que aún tienen la frecuencia rebajada
        // pendiente de restauración manual (la rebaja sobrevive al fin del bloqueo).
        setSpamBlocks(res.channels.filter((c: any) => c.blocked || c.freq_reduced))
      } else {
        setSpamBlocks([])
      }
    } catch { /* spam block info is optional, not critical */ }
  }

  async function unblockChannel(cid: number) {
    if (!window.confirm('¿Desbloquear este canal? Solo hazlo si has verificado en YouTube Studio que la penalización ya no está activa.')) return
    try {
      await api.unblockSpamChannel(cid)
      await loadSpamBlocks()
    } catch (e: any) {
      setError(e.message || 'Error al desbloquear')
    }
  }

  async function restoreFrequency(cid: number) {
    if (!window.confirm('¿Restaurar la frecuencia de publicación original? Hazlo solo si la penalización de spam ha cesado.')) return
    try {
      await api.restoreSpamFrequency(cid)
      await loadSpamBlocks()
    } catch (e: any) {
      setError(e.message || 'Error al restaurar frecuencia')
    }
  }

  async function loadChannels() {
    setLoading(true)
    try {
      const data = await api.getChannels()
      setChannels(data)
      // Load stats for all channels (snapshot from DB)
      try {
        const statsRes = await api.getAllChannelStats()
        if (statsRes?.ok && statsRes.channels) {
          const map: Record<number, any> = {}
          statsRes.channels.forEach((s: any) => { map[s.channel_id] = s })
          setChannelStats(map)
        }
      } catch { /* stats are optional, not critical */ }
    } catch (e) { console.error(e) }
    setLoading(false)
  }

  function openNew() {
    setEditing(null)
    setForm({ name: '', slug: '', youtube_handle: '', google_account: '' })
    setError('')
    setShowForm(true)
  }

  function openEdit(ch: any) {
    setEditing(ch)
    setForm({
      name: ch.name, slug: ch.slug,
      youtube_handle: ch.config_json?.YOUTUBE_HANDLE || ch.youtube_handle || '',
      google_account: ch.google_account || '',
    })
    setError('')
    setShowForm(true)
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    setError('')
    if (!form.name.trim() || !form.slug.trim()) {
      setError('Nombre y slug son obligatorios')
      return
    }
    try {
      if (editing) {
        await api.updateChannel(editing.id, {
          name: form.name, slug: form.slug,
          google_account: form.google_account || undefined,
        })
      } else {
        await api.createChannel({
          name: form.name, slug: form.slug,
          youtube_handle: form.youtube_handle || undefined,
          google_account: form.google_account || undefined,
        })
      }
      setShowForm(false)
      loadChannels()
    } catch (e: any) {
      setError(e.message || 'Error al guardar')
    }
  }

  async function handleDelete(id: number) {
    if (!confirm('¿Eliminar este canal y todos sus videos?')) return
    try {
      await api.deleteChannel(id)
      loadChannels()
    } catch (e: any) {
      alert(e.message)
    }
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="animate-spin rounded-full h-8 w-8 border-2 border-neon-red border-t-transparent" />
      </div>
    )
  }

  return (
    <div className="max-w-5xl mx-auto space-y-6 animate-fade-in">
      {/* Header */}
      <div className="flex flex-col sm:flex-row items-start sm:items-center justify-between gap-3">
        <h2 className="font-display text-xl sm:text-2xl font-bold text-white flex items-center gap-3">
          <Radio size={22} className="text-neon-red" />
          Canales
        </h2>
        <button
          onClick={openNew}
          className="flex items-center gap-2 px-3 py-2 bg-neon-red text-white rounded-lg hover:bg-neon-red/80 transition-all text-sm font-medium w-full sm:w-auto justify-center"
        >
          <Plus size={16} />
          Nuevo Canal
        </button>
      </div>

      {/* Spam-blocked channels banner */}
      {spamBlocks.length > 0 && (
        <div className="glass rounded-xl p-4 border border-neon-red/40 space-y-2">
          <div className="flex items-center gap-2">
            <Zap size={16} className="text-neon-red" />
            <h3 className="text-sm font-semibold text-white">Canales bloqueados por spam de YouTube</h3>
          </div>
          <p className="text-xs text-gray-400">
            Las subidas (shorts y vídeos) de estos canales están bloqueadas automáticamente hasta que expire la
            penalización. Tras el bloqueo, la frecuencia de publicación queda rebajada hasta que la restaures
            manualmente.
          </p>
          {spamBlocks.map((sb: any) => (
            <div key={sb.channel_id} className="flex items-center justify-between gap-3 flex-wrap bg-dark-700/50 rounded-lg px-3 py-2">
              <div className="flex items-center gap-2 text-sm">
                <span className="text-neon-red font-semibold">{sb.name || sb.slug}</span>
                <span className="text-gray-400 text-xs">
                  {sb.blocked
                    ? `(${sb.strikes} strike${sb.strikes !== 1 ? 's' : ''}) · bloqueado hasta +${sb.restan_h}h restantes`
                    : 'bloqueo expirado'}
                  {sb.freq_reduced ? ' · frecuencia rebajada' : ''}
                </span>
              </div>
              <div className="flex items-center gap-2">
                {sb.freq_reduced && (
                  <button
                    onClick={() => restoreFrequency(sb.channel_id)}
                    className="px-3 py-1 text-xs bg-amber-500/10 text-amber-400 border border-amber-500/40 rounded-lg hover:bg-amber-500 hover:text-white transition-all"
                  >
                    Restaurar frecuencia
                  </button>
                )}
                {sb.blocked && (
                  <button
                    onClick={() => unblockChannel(sb.channel_id)}
                    className="px-3 py-1 text-xs bg-neon-red/20 text-neon-red border border-neon-red/40 rounded-lg hover:bg-neon-red hover:text-white transition-all"
                  >
                    Desbloquear
                  </button>
                )}
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Stats summary row */}
      {Object.keys(channelStats).length > 0 && (
        <div className="glass rounded-xl p-4 flex items-center justify-between flex-wrap gap-3">
          <div className="flex items-center gap-3">
            <Eye size={18} className="text-neon-red" />
            <span className="text-sm text-gray-300">Totales de todos los canales</span>
          </div>
          <div className="flex items-center gap-3 text-xs sm:text-sm flex-wrap">
            <span className="font-display text-lg text-neon-gold font-bold tabular-nums">
              {formatShortNumber(
                Object.values(channelStats).reduce((sum: number, s: any) => sum + (s.total_views || 0), 0)
              )}
            </span>
            <span className="text-gray-500">vistas YT</span>
            <span className="text-gray-600 hidden sm:inline">|</span>
            <span className="font-display text-sm text-neon-cyan font-bold tabular-nums">
              {formatShortNumber(
                Object.values(channelStats).reduce((sum: number, s: any) => sum + (s.longform_views || 0), 0)
              )}
            </span>
            <span className="text-gray-500 text-[10px]">vídeos</span>
            <span className="text-gray-600 hidden sm:inline">|</span>
            <span className="font-display text-sm text-neon-purple font-bold tabular-nums">
              {formatShortNumber(
                Object.values(channelStats).reduce((sum: number, s: any) => sum + (s.shorts_views || 0), 0)
              )}
            </span>
            <span className="text-gray-500 text-[10px]">shorts</span>
            <span className="text-gray-600 hidden sm:inline">|</span>
            <span className="font-display text-lg text-neon-pink font-bold tabular-nums">
              {formatShortNumber(
                Object.values(channelStats).reduce((sum: number, s: any) => sum + (s.subscribers || 0), 0)
              )}
            </span>
            <span className="text-gray-500 hidden sm:inline">subs</span>
            <span className="text-gray-600 hidden sm:inline">|</span>
            <span className="font-display text-lg text-neon-cyan font-bold tabular-nums">
              {formatShortNumber(
                Object.values(channelStats).reduce((sum: number, s: any) => sum + (s.shorts_published || 0), 0)
              )}
            </span>
            <span className="text-gray-500 hidden sm:inline">shorts</span>
          </div>
        </div>
      )}

      {/* Form Modal */}
      {showForm && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm" onClick={() => setShowForm(false)}>
          <div className="glass rounded-xl p-5 sm:p-6 w-full max-w-md mx-4 sm:mx-0 space-y-4 animate-slide-up" onClick={e => e.stopPropagation()}>
            <h3 className="font-display text-lg font-semibold text-white">
              {editing ? 'Editar Canal' : 'Nuevo Canal'}
            </h3>
            <form onSubmit={handleSubmit} className="space-y-3">
              <div>
                <label className="block text-xs text-gray-400 mb-1">Nombre</label>
                <input
                  type="text"
                  value={form.name}
                  onChange={e => setForm({ ...form, name: e.target.value })}
                  className="w-full px-3 py-2 bg-dark-700 border border-surface-border rounded-lg text-white text-sm focus:outline-none focus:border-neon-red transition-colors"
                  placeholder="Canal de Misterio"
                  autoFocus
                />
              </div>
              <div>
                <label className="block text-xs text-gray-400 mb-1">Slug (identificador)</label>
                <input
                  type="text"
                  value={form.slug}
                  onChange={e => setForm({ ...form, slug: e.target.value.toLowerCase().replace(/[^a-z0-9_-]/g, '') })}
                  className="w-full px-3 py-2 bg-dark-700 border border-surface-border rounded-lg text-white text-sm font-mono focus:outline-none focus:border-neon-red transition-colors"
                  placeholder="canal-misterio"
                />
              </div>
              <div>
                <label className="block text-xs text-gray-400 mb-1">YouTube Handle (opcional)</label>
                <input
                  type="text"
                  value={form.youtube_handle}
                  onChange={e => setForm({ ...form, youtube_handle: e.target.value })}
                  className="w-full px-3 py-2 bg-dark-700 border border-surface-border rounded-lg text-white text-sm focus:outline-none focus:border-neon-red transition-colors"
                  placeholder="@MiCanal"
                />
              </div>
              <div>
                <label className="block text-xs text-gray-400 mb-1">Cuenta Google (opcional)</label>
                <input
                  type="text"
                  value={form.google_account}
                  onChange={e => setForm({ ...form, google_account: e.target.value })}
                  className="w-full px-3 py-2 bg-dark-700 border border-surface-border rounded-lg text-white text-sm focus:outline-none focus:border-neon-red transition-colors"
                  placeholder="micuenta (sin @gmail.com)"
                />
                <p className="text-[10px] text-gray-500 mt-1">Para automatizacion de YouTube Studio (marcar IA, end screens)</p>
              </div>
              {error && <p className="text-sm text-red-400">{error}</p>}
              <div className="flex gap-2 pt-2">
                <button type="submit" className="flex-1 px-4 py-2 bg-neon-red text-white rounded-lg hover:bg-neon-red/80 text-sm font-medium">
                  {editing ? 'Guardar' : 'Crear'}
                </button>
                <button type="button" onClick={() => setShowForm(false)} className="px-4 py-2 bg-dark-600 text-gray-300 rounded-lg hover:bg-dark-500 text-sm">
                  Cancelar
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* Channels Grid */}
      {channels.length === 0 ? (
        <div className="text-center py-16 glass rounded-xl">
          <Radio size={48} className="mx-auto mb-4 opacity-20 text-gray-600" />
          <p className="text-gray-500 mb-2">No hay canales creados</p>
          <p className="text-sm text-gray-600 mb-4">Crea tu primer canal para empezar a generar videos</p>
          <button onClick={openNew} className="px-4 py-2 bg-neon-red text-white rounded-lg hover:bg-neon-red/80 text-sm">
            Crear Canal
          </button>
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
          {channels.map((ch: any) => (
            <div key={ch.id} className="glass rounded-xl p-5 neon-border hover:border-neon-red/60 transition-all duration-300 group">
              <div className="flex items-start justify-between mb-3">
                <div>
                  <h3 className="font-display text-lg font-semibold text-white">{ch.name}</h3>
                  <p className="text-xs text-gray-500 font-mono">{ch.slug}</p>
                </div>
                <div className="flex gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
                  <button onClick={() => openEdit(ch)} className="p-1.5 rounded hover:bg-dark-600 text-gray-400 hover:text-white transition-colors">
                    <Edit2 size={14} />
                  </button>
                  <button onClick={() => handleDelete(ch.id)} className="p-1.5 rounded hover:bg-red-900/30 text-gray-400 hover:text-red-400 transition-colors">
                    <Trash2 size={14} />
                  </button>
                </div>
              </div>
              <div className="flex items-center gap-3 text-sm text-gray-400 mb-3">
                <Film size={14} className="text-neon-red" />
                <span>{ch.active ? 'Activo' : 'Inactivo'}</span>
                <span>·</span>
                <span>{formatDate(ch.created_at)}</span>
              </div>
              {/* Channel stats grid */}
              <div className="grid grid-cols-3 gap-x-2 gap-y-1.5 mb-3">
                <div className="flex items-center gap-1.5 text-[11px] text-gray-400">
                  <Eye size={11} className="text-neon-cyan shrink-0" />
                  <span className="font-mono text-neon-cyan tabular-nums">
                    {channelStats[ch.id] ? formatShortNumber(channelStats[ch.id].longform_views || 0) : '—'}
                  </span>
                  <span className="hidden sm:inline">v. vídeos</span>
                </div>
                <div className="flex items-center gap-1.5 text-[11px] text-gray-400">
                  <Zap size={11} className="text-neon-purple shrink-0" />
                  <span className="font-mono text-neon-purple tabular-nums">
                    {channelStats[ch.id] ? formatShortNumber(channelStats[ch.id].shorts_views || 0) : '—'}
                  </span>
                  <span className="hidden sm:inline">v. shorts</span>
                </div>
                <div className="flex items-center gap-1.5 text-[11px] text-gray-400">
                  <Eye size={11} className="text-neon-gold shrink-0" />
                  <span className="font-mono text-neon-gold tabular-nums">
                    {channelStats[ch.id] ? formatShortNumber(channelStats[ch.id].total_views || 0) : '—'}
                  </span>
                  <span className="hidden sm:inline">v. YT</span>
                </div>
                <div className="flex items-center gap-1.5 text-[11px] text-gray-400">
                  <Users size={11} className="text-neon-pink shrink-0" />
                  <span className="font-mono text-neon-pink tabular-nums">
                    {channelStats[ch.id] ? formatShortNumber(channelStats[ch.id].subscribers || 0) : '—'}
                  </span>
                  <span className="hidden sm:inline">subs</span>
                </div>
                <div className="flex items-center gap-1.5 text-[11px] text-gray-400">
                  <Film size={11} className="text-neon-cyan shrink-0" />
                  <span className="font-mono tabular-nums">
                    {channelStats[ch.id] ? (channelStats[ch.id].video_count ?? '—') : '—'}
                  </span>
                  <span className="hidden sm:inline">vídeos</span>
                </div>
                <div className="flex items-center gap-1.5 text-[11px] text-gray-400">
                  <Zap size={11} className="text-neon-purple shrink-0" />
                  <span className="font-mono tabular-nums">
                    {channelStats[ch.id] ? (channelStats[ch.id].shorts_published ?? '—') : '—'}
                  </span>
                  <span className="hidden sm:inline">shorts</span>
                </div>
              </div>
              <Link
                to={`/channels/${ch.id}`}
                className="block w-full text-center px-4 py-2 bg-neon-red/10 border border-neon-red/30 text-neon-red rounded-lg hover:bg-neon-red/20 transition-all text-sm font-medium mb-2"
              >
                Gestionar Canal →
              </Link>
              <div className="grid grid-cols-3 gap-2">
                <Link
                  to="/scheduling"
                  className="text-center px-3 py-1.5 bg-neon-gold/10 border border-neon-gold/30 text-neon-gold rounded-lg text-xs font-medium hover:bg-neon-gold/20 transition-colors flex items-center justify-center gap-1"
                >
                  <Calendar size={12} /> Programar
                </Link>
                {ch.yt_channel_url ? (
                  <a href={ch.yt_channel_url} target="_blank" rel="noopener noreferrer"
                    className="text-center px-3 py-1.5 bg-red-600/10 border border-red-600/30 text-red-400 rounded-lg text-xs font-medium hover:bg-red-600/20 transition-colors flex items-center justify-center gap-1">
                    <Youtube size={12} /> YT
                  </a>
                ) : (
                  <span className="text-center px-3 py-1.5 text-gray-600 text-xs flex items-center justify-center gap-1 cursor-not-allowed">
                    <Youtube size={12} /> —
                  </span>
                )}
                {ch.yt_studio_url ? (
                  <a href={ch.yt_studio_url} target="_blank" rel="noopener noreferrer"
                    className="text-center px-3 py-1.5 bg-neon-cyan/10 border border-neon-cyan/30 text-neon-cyan rounded-lg text-xs font-medium hover:bg-neon-cyan/20 transition-colors flex items-center justify-center gap-1">
                    <ExternalLink size={12} /> Studio
                  </a>
                ) : (
                  <span className="text-center px-3 py-1.5 text-gray-600 text-xs flex items-center justify-center gap-1 cursor-not-allowed">
                    <ExternalLink size={12} /> —
                  </span>
                )}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
