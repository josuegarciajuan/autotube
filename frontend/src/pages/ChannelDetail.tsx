import { useState, useEffect } from 'react'
import { useParams, Link } from 'react-router-dom'
import { api, formatDate, formatDuration, formatShortNumber, apiUrl } from '../lib/api'
import { useGeneration } from '../context/GenerationContext'
import { ArrowLeft, Wand2, Upload, Play, AlertCircle, Calendar, Youtube, Edit3, Save, Users, Video, Image, Settings, RefreshCw, Zap, Loader2, Key, Link2, Clipboard, ExternalLink } from 'lucide-react'
import { CONFIG_SECTIONS, type ConfigSection, type ConfigField } from '../types/channel'

export default function ChannelDetail() {
  const { id } = useParams<{ id: string }>()
  const channelId = Number(id)

  const [channel, setChannel] = useState<any>(null)
  const [videos, setVideos] = useState<any[]>([])
  const [loading, setLoading] = useState(true)
  const [generating, setGenerating] = useState(false)
  const [editingProfile, setEditingProfile] = useState(false)
  const [profileForm, setProfileForm] = useState({ name: '', description: '', banner_url: '', avatar_url: '', yt_channel_url: '' })
  const [saving, setSaving] = useState(false)
  const [videoStats, setVideoStats] = useState<Record<string, any>>({})
  const [showConfig, setShowConfig] = useState(false)
  const [syncing, setSyncing] = useState(false)
  const [editingConfig, setEditingConfig] = useState(false)
  const [editConfig, setEditConfig] = useState<Record<string, any>>({})
  
  // Auth state
  const [authStatus, setAuthStatus] = useState<any>(null)
  const [authUrl, setAuthUrl] = useState('')
  const [authCode, setAuthCode] = useState('')
  const [showAuthModal, setShowAuthModal] = useState(false)
  const [authLoading, setAuthLoading] = useState(false)

  // Manual setup state
  const [manualSetup, setManualSetup] = useState<any>(null)
  const [showManualSetup, setShowManualSetup] = useState(false)
  const [syncResult, setSyncResult] = useState<any>(null)

  const { setActiveJob } = useGeneration()

  useEffect(() => {
    async function load() {
      try {
        const [ch, vids] = await Promise.all([
          api.getChannel(channelId),
          api.getChannelVideos(channelId),
        ])
        setChannel(ch)
        setVideos(vids)
        setProfileForm({ name: ch.name || '', description: ch.description || '', banner_url: ch.banner_url || '', avatar_url: ch.avatar_url || '', yt_channel_url: ch.yt_channel_url || '' })
      } catch (e) { console.error(e) }
      setLoading(false)
    }
    load()
  }, [channelId])

  // Check auth status
  useEffect(() => {
    api.getAuthStatus(channelId).then(setAuthStatus).catch(() => {})
  }, [channelId])

  // Fetch YouTube stats for uploaded videos
  useEffect(() => {
    const ytIds = videos.filter((v: any) => v.yt_video_id).map((v: any) => v.yt_video_id)
    if (ytIds.length === 0) return
    api.getVideoStats(ytIds).then(stats => setVideoStats(stats)).catch(() => {})
  }, [videos])

  // Poll videos list when generating (since we use global progress bar now)
  useEffect(() => {
    if (!generating) return
    const interval = setInterval(async () => {
      try { const vids = await api.getChannelVideos(channelId); setVideos(vids) } catch {}
    }, 5000)
    return () => clearInterval(interval)
  }, [generating, channelId])

  async function handleGenerate() {
    setGenerating(true)
    try {
      const result = await api.generateVideo({ channel_id: channelId, action: 'generate_and_upload' })
      setActiveJob({
        jobId: result.job_id,
        channelId,
        channelName: channel?.name || 'Canal',
        action: 'generate_and_upload',
      })
      pollForCompletion(result.job_id)
    } catch (e: any) { alert('Error: ' + e.message); setGenerating(false) }
  }

  async function pollForCompletion(jobId: number) {
    const check = async () => {
      try {
        const job = await api.getJob(jobId)
        if (job.status === 'completed' || job.status === 'failed') {
          setGenerating(false)
          setActiveJob(null)
          const vids = await api.getChannelVideos(channelId)
          setVideos(vids)
        } else {
          setTimeout(check, 2000)
        }
      } catch {
        setTimeout(check, 3000)
      }
    }
    setTimeout(check, 2000)
  }

  async function handleUpload(videoId: number) {
    try { await api.uploadVideo(videoId); alert('Subida iniciada'); const vids = await api.getChannelVideos(channelId); setVideos(vids) } catch (e: any) { alert('Error: ' + e.message) }
  }

  async function handleSaveProfile() {
    setSaving(true)
    try {
      await api.updateChannelProfile(channelId, profileForm)
      const ch = await api.getChannel(channelId)
      setChannel(ch)
      setEditingProfile(false)
    } catch (e: any) { alert('Error: ' + e.message) }
    setSaving(false)
  }

  async function handleSyncYouTube() {
    setSyncing(true)
    try {
      const result = await api.syncYoutube(channelId)
      setSyncResult(result)
      if (result.manual_setup_required && result.manual_setup_required.length > 0) {
        setManualSetup(result)
        setShowManualSetup(true)
      }
      const updated = result.api_updated || []
      const fields = updated.length > 0 ? updated.join(', ') : 'nada que actualizar'
      alert(`Sincronización completada.\nAPI: ${fields}\n${result.manual_setup_required?.length ? 'Revisa la configuración manual para completar el setup.' : ''}`)
    } catch (e: any) { alert('Error: ' + e.message) }
    setSyncing(false)
  }

  async function handleStartAuth() {
    setAuthLoading(true)
    try {
      const res = await api.startAuth(channelId)
      setAuthUrl(res.auth_url)
      setShowAuthModal(true)
    } catch (e: any) { alert('Error: ' + e.message) }
    setAuthLoading(false)
  }

  async function handleSubmitAuthCode() {
    if (!authCode.trim()) return
    setAuthLoading(true)
    try {
      const res = await api.submitAuthCode(channelId, authCode.trim())
      alert(res.message || '✅ Conectado')
      setShowAuthModal(false)
      setAuthCode('')
      api.getAuthStatus(channelId).then(setAuthStatus)
      // Reload channel data
      const ch = await api.getChannel(channelId)
      setChannel(ch)
    } catch (e: any) { alert('Error: ' + e.message) }
    setAuthLoading(false)
  }

  async function handleGetManualSetup() {
    try {
      const res = await api.getManualSetup(channelId)
      setManualSetup(res)
      setShowManualSetup(true)
    } catch (e: any) { alert('Error: ' + e.message) }
  }

  async function handleSaveConfig() {
    setSyncing(true)
    try {
      const res = await fetch(`api/channels/${channelId}/config`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ config: editConfig }),
      })
      if (!res.ok) throw new Error('Save failed')
      const ch = await api.getChannel(channelId)
      setChannel(ch)
      setEditingConfig(false)
    } catch (e: any) { alert('Error: ' + e.message) }
    setSyncing(false)
  }

  function startEditingConfig() {
    setEditConfig({ ...(channel.config_json || {}) })
    setEditingConfig(true)
  }

  function updateConfigField(key: string, value: any) {
    setEditConfig(prev => ({ ...prev, [key]: value }))
  }

  function renderEditField(field: ConfigField, value: any): React.ReactNode {
    if (field.type === 'boolean') {
      return (
        <select value={value ? 'true' : 'false'} onChange={e => updateConfigField(field.key, e.target.value === 'true')}
          className="bg-dark-900 border border-surface-border text-white text-xs rounded px-1 py-0.5 w-full max-w-[120px]">
          <option value="true">✅ Sí</option>
          <option value="false">❌ No</option>
        </select>
      )
    }
    if (field.type === 'select' && field.options) {
      return (
        <select value={value || ''} onChange={e => updateConfigField(field.key, e.target.value)}
          className="bg-dark-900 border border-surface-border text-white text-xs rounded px-1 py-0.5 w-full">
          {field.options.map(opt => (
            <option key={opt.value} value={opt.value}>{opt.label}</option>
          ))}
        </select>
      )
    }
    if (field.type === 'number') {
      return <input type="number" value={value || ''} onChange={e => updateConfigField(field.key, Number(e.target.value))}
        className="bg-dark-900 border border-surface-border text-white text-xs rounded px-1 py-0.5 w-full max-w-[100px]" />
    }
    if (field.type === 'text' && typeof value === 'string' && value.length > 50) {
      return <textarea value={value || ''} onChange={e => updateConfigField(field.key, e.target.value)} rows={1}
        className="bg-dark-900 border border-surface-border text-white text-xs rounded px-1 py-0.5 w-full resize-none" />
    }
    return <input type="text" value={value || ''} onChange={e => updateConfigField(field.key, e.target.value)}
      className="bg-dark-900 border border-surface-border text-white text-xs rounded px-1 py-0.5 w-full" />
  }

  async function handleSyncConfig() {
    setSyncing(true)
    try {
      await api.syncChannelConfig(channelId)
      const ch = await api.getChannel(channelId)
      setChannel(ch)
    } catch (e: any) { alert('Error al sincronizar config: ' + e.message) }
    setSyncing(false)
  }

  function renderConfigValue(field: ConfigField, config: Record<string, any>): React.ReactNode {
    const value = config[field.key]
    if (value === undefined || value === null) return <span className="text-gray-600">—</span>
    if (field.type === 'boolean') return <span className={value ? 'text-green-400' : 'text-gray-500'}>{value ? '✅ Sí' : '❌ No'}</span>
    if (field.type === 'select' && field.options) {
      const opt = field.options.find(o => o.value === value)
      return <span className="text-sm text-gray-300">{opt?.label || String(value)}</span>
    }
    if (field.type === 'list' && Array.isArray(value)) {
      return (
        <div className="flex flex-wrap gap-1">
          {value.length === 0 ? <span className="text-gray-600">—</span> : value.slice(0, 8).map((item, i) => (
            <span key={i} className="text-xs bg-dark-700 px-1.5 py-0.5 rounded text-gray-300">{String(item).substring(0, 40)}</span>
          ))}
          {value.length > 8 && <span className="text-xs text-gray-500">+{value.length - 8} más</span>}
        </div>
      )
    }
    if (field.type === 'number' || field.type === 'text') {
      const s = String(value)
      return <span className="text-sm text-gray-300">{s.length > 100 ? s.substring(0, 100) + '...' : s}</span>
    }
    if (field.type === 'dict') return <span className="text-xs text-gray-500">{JSON.stringify(value).substring(0, 80)}...</span>
    return <span className="text-sm text-gray-300">{String(value).substring(0, 100)}</span>
  }

  if (loading) {
    return <div className="flex items-center justify-center h-64"><div className="animate-spin rounded-full h-8 w-8 border-2 border-neon-red border-t-transparent" /></div>
  }

  if (!channel) {
    return <div className="text-center py-16 text-gray-500"><AlertCircle size={48} className="mx-auto mb-4 opacity-30" />Canal no encontrado</div>
  }

  const uploadedCount = videos.filter((v: any) => v.yt_video_id).length

  return (
    <div className="max-w-6xl mx-auto animate-fade-in">
      {/* --- YouTube-style Banner --- */}
      <div className="relative">
        <div className="w-full h-40 md:h-52 rounded-xl overflow-hidden bg-gradient-to-r from-dark-700 via-dark-600 to-neon-red/20">
          {channel.banner_url ? (
            <img src={channel.banner_url} alt="Banner" className="w-full h-full object-cover" />
          ) : (
            <div className="w-full h-full flex items-center justify-center">
              <Image size={48} className="text-gray-700" />
            </div>
          )}
        </div>
        
        {/* Avatar + Info */}
        <div className="px-4 md:px-6 -mt-10 flex items-end gap-4 md:gap-5">
          <div className="w-20 h-20 md:w-24 md:h-24 rounded-full border-4 border-dark-900 bg-dark-700 overflow-hidden shrink-0">
            {channel.avatar_url ? (
              <img src={channel.avatar_url} alt="Avatar" className="w-full h-full object-cover" />
            ) : (
              <div className="w-full h-full flex items-center justify-center bg-neon-red/20">
                <Youtube size={28} className="text-neon-red" />
              </div>
            )}
          </div>
          
          <div className="flex-1 pb-1 min-w-0">
            <h1 className="font-display text-xl md:text-2xl font-bold text-white truncate">{channel.name}</h1>
            <div className="flex items-center gap-3 text-xs text-gray-400 mt-0.5 flex-wrap">
              <span className="flex items-center gap-1"><Users size={12} />{uploadedCount} videos</span>
              <span className="flex items-center gap-1"><Video size={12} />{videos.length} total</span>
              <span className="hidden sm:inline">·</span>
              <span className="hidden sm:inline">{channel.slug}</span>
            </div>
            {channel.description && (
              <p className="text-xs text-gray-400 mt-1.5 line-clamp-2">{channel.description}</p>
            )}
          </div>

          <div className="flex gap-2 pb-1 shrink-0">
            {channel.yt_channel_url && (
              <a href={channel.yt_channel_url} target="_blank" rel="noopener noreferrer"
                className="flex items-center gap-1 px-3 py-1.5 bg-red-600 text-white rounded-full text-xs font-medium hover:bg-red-700 transition-colors">
                <Youtube size={14} /> YouTube
              </a>
            )}
            <button onClick={() => setEditingProfile(true)}
              className="flex items-center gap-1 px-3 py-1.5 bg-dark-700 border border-surface-border text-gray-300 rounded-full text-xs hover:bg-dark-600 transition-colors">
              <Edit3 size={12} /> Editar perfil
            </button>
            <button onClick={() => setShowConfig(!showConfig)}
              className="flex items-center gap-1 px-3 py-1.5 bg-dark-700 border border-neon-cyan/30 text-neon-cyan rounded-full text-xs hover:bg-dark-600 transition-colors">
              <Settings size={12} /> {showConfig ? 'Ocultar' : 'Config'}
            </button>
          </div>
        </div>
      </div>

      {/* --- Quick actions bar --- */}
      <div className="flex items-center gap-2 mt-4 mb-6 flex-wrap">
        <Link to="/scheduling"
          className="flex items-center gap-1.5 px-4 py-2 bg-neon-gold/10 border border-neon-gold/30 text-neon-gold rounded-lg text-sm font-medium hover:bg-neon-gold/20 transition-colors">
          <Calendar size={14} /> Programar
        </Link>
        <button onClick={handleStartAuth} disabled={authLoading}
          className={`flex items-center gap-1.5 px-4 py-2 rounded-lg text-sm font-medium transition-colors ${
            authStatus?.authenticated 
              ? 'bg-green-600/10 border border-green-600/30 text-green-400' 
              : 'bg-neon-cyan/10 border border-neon-cyan/30 text-neon-cyan hover:bg-neon-cyan/20'
          }`}>
          <Key size={14} /> {authStatus?.authenticated ? '✅ Conectado' : 'Conectar YouTube'}
        </button>
        <button onClick={handleSyncYouTube} disabled={syncing}
          className="flex items-center gap-1.5 px-4 py-2 bg-red-600/10 border border-red-600/30 text-red-400 rounded-lg text-sm font-medium hover:bg-red-600/20 transition-colors">
          <RefreshCw size={14} className={syncing ? 'animate-spin' : ''} /> {syncing ? 'Syncing...' : 'Sync YouTube'}
        </button>
        <button onClick={handleGetManualSetup}
          className="flex items-center gap-1.5 px-4 py-2 bg-dark-600 border border-surface-border text-gray-400 rounded-lg text-sm font-medium hover:bg-dark-500 transition-colors">
          <Clipboard size={14} /> Setup Manual
        </button>
        <Link to="/channels"
          className="flex items-center gap-1.5 px-3 py-2 text-sm text-gray-400 hover:text-white transition-colors">
          <ArrowLeft size={14} /> Canales
        </Link>
      </div>

      {/* --- Generation Panel --- */}
      <div className="glass rounded-xl p-5 space-y-4 mb-6">
        <h3 className="font-display text-lg font-semibold text-white flex items-center gap-2">
          <Wand2 size={20} className="text-neon-gold" /> Generar Video
        </h3>
        {generating ? (
          <div className="flex items-center gap-3 p-4 bg-dark-700/50 rounded-lg">
            <Loader2 size={20} className="text-neon-gold animate-spin" />
            <div>
              <p className="text-sm font-medium text-white">Generando y subiendo video...</p>
              <p className="text-xs text-gray-400">El pipeline completo (script → TTS → video → upload) está en marcha. El progreso se muestra en la barra inferior.</p>
            </div>
          </div>
        ) : (
          <button onClick={handleGenerate} disabled={generating}
            className="w-full py-4 bg-gradient-to-r from-neon-red to-red-600 text-white rounded-xl font-display font-semibold text-lg hover:shadow-lg hover:shadow-neon-red/20 transition-all duration-300 disabled:opacity-50 flex items-center justify-center gap-3">
            <Wand2 size={22} /> {generating ? 'Iniciando...' : 'Generar Video'}
          </button>
        )}
        <p className="text-xs text-gray-500 text-center">Genera y sube automáticamente a YouTube. Recibirás una notificación al terminar.</p>
      </div>

      {/* --- Video Grid --- */}
      <section>
        <div className="flex items-center gap-2 mb-4">
          <button className="px-4 py-1.5 bg-dark-700 text-white rounded-lg text-sm font-medium">Videos</button>
          <button className="px-4 py-1.5 text-gray-500 text-sm hover:text-white">Shorts</button>
          <button className="px-4 py-1.5 text-gray-500 text-sm hover:text-white">En directo</button>
        </div>
        {videos.length === 0 ? (
          <div className="text-center py-16 glass rounded-xl">
            <Video size={48} className="mx-auto mb-4 opacity-20 text-gray-600" />
            <p className="text-gray-500">No hay videos en este canal</p>
            <p className="text-xs text-gray-600 mt-1">Genera tu primer video usando el panel de arriba</p>
          </div>
        ) : (
          <div className="grid gap-4 sm:grid-cols-2 md:grid-cols-3">
            {videos.map((v: any) => (
              <Link key={v.id} to={`/videos/${v.id}/edit`} className="group">
                <div className="relative aspect-video rounded-xl overflow-hidden bg-dark-700 mb-2">
                  {v.thumbnail_path ? (
                    <img src={apiUrl(`/thumbnail/${v.id}?v=${v.updated_at || v.id}`)} alt={v.titulo_final} className="w-full h-full object-cover group-hover:scale-110 transition-transform duration-500" />
                  ) : (
                    <div className="w-full h-full flex items-center justify-center bg-gradient-to-br from-dark-700 to-dark-900"><Video size={28} className="text-gray-700" /></div>
                  )}
                  {v.duracion_seg && <span className="absolute bottom-1.5 right-1.5 bg-black/85 text-white text-[11px] px-1.5 py-0.5 rounded font-mono">{formatDuration(v.duracion_seg)}</span>}
                  <span className={`absolute top-1.5 left-1.5 text-[10px] px-1.5 py-0.5 rounded font-medium ${v.status === 'uploaded' ? 'bg-green-600/90 text-white' : v.status === 'ready' ? 'bg-neon-cyan/90 text-dark-900' : v.status === 'generating' ? 'bg-blue-600/90 text-white' : v.status === 'error' ? 'bg-red-600/90 text-white' : 'bg-gray-700/90 text-gray-300'}`}>
                    {v.status === 'uploaded' ? 'Subido' : v.status === 'ready' ? 'Listo' : v.status === 'generating' ? 'Generando' : v.status || 'Borrador'}
                  </span>
                  <div className="absolute inset-0 flex items-center justify-center opacity-0 group-hover:opacity-100 transition-opacity duration-300 bg-black/20">
                    <div className="w-12 h-12 rounded-full bg-neon-red/90 flex items-center justify-center shadow-lg"><Play size={20} className="text-white ml-0.5" /></div>
                  </div>
                </div>
                <div>
                  <p className="text-sm font-medium text-white leading-tight line-clamp-2 group-hover:text-neon-red transition-colors">{v.titulo_final || 'Video sin título'}</p>
                  <p className="text-xs text-gray-500 mt-1">{channel.name}</p>
                  <div className="flex items-center gap-1.5 mt-0.5 text-xs text-gray-600">
                    <span>{formatDate(v.created_at)}</span>
                    {v.yt_url && <><span>·</span><a href={v.yt_url} target="_blank" rel="noopener noreferrer" className="text-neon-red hover:underline flex items-center gap-0.5" onClick={e => e.stopPropagation()}><Youtube size={10} /> YT</a></>}
                  </div>
                  {/* YouTube Stats */}
                  {v.yt_video_id && videoStats[v.yt_video_id] && (
                    <div className="flex items-center gap-2 mt-0.5 text-[11px] text-gray-500">
                      <span>{formatShortNumber(videoStats[v.yt_video_id].viewCount || '0')} vistas</span>
                      <span>·</span>
                      <span>{formatShortNumber(videoStats[v.yt_video_id].likeCount || '0')} likes</span>
                    </div>
                  )}
                  <div className="flex gap-1 mt-1.5 opacity-0 group-hover:opacity-100 transition-opacity">
                    {v.status === 'ready' && !v.yt_video_id && <button onClick={e => { e.preventDefault(); handleUpload(v.id) }} className="text-[11px] text-neon-red bg-neon-red/10 px-2 py-0.5 rounded hover:bg-neon-red/20">Subir a YT</button>}
                    {v.yt_video_id && <button onClick={e => { e.preventDefault(); handleUpload(v.id) }} className="text-[11px] text-neon-gold bg-neon-gold/10 px-2 py-0.5 rounded hover:bg-neon-gold/20">Resubir</button>}
                  </div>
                </div>
              </Link>
            ))}
          </div>
        )}
      </section>

      {/* --- Edit Profile Modal --- */}
      {editingProfile && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm" onClick={() => setEditingProfile(false)}>
          <div className="glass rounded-xl p-6 w-full max-w-lg space-y-4 animate-slide-up max-h-[90vh] overflow-y-auto" onClick={e => e.stopPropagation()}>
            <h3 className="font-display text-lg font-semibold text-white flex items-center gap-2"><Edit3 size={18} className="text-neon-red" /> Editar Perfil del Canal</h3>
            <div className="space-y-3">
              <div><label className="block text-xs text-gray-400 mb-1">Nombre del canal</label>
                <input type="text" value={profileForm.name} onChange={e => setProfileForm({ ...profileForm, name: e.target.value })}
                  className="w-full px-3 py-2 bg-dark-700 border border-surface-border rounded-lg text-white text-sm focus:outline-none focus:border-neon-red" /></div>
              <div><label className="block text-xs text-gray-400 mb-1">Descripción</label>
                <textarea value={profileForm.description || ''} onChange={e => setProfileForm({ ...profileForm, description: e.target.value })} rows={3}
                  className="w-full px-3 py-2 bg-dark-700 border border-surface-border rounded-lg text-white text-sm focus:outline-none focus:border-neon-red resize-none" /></div>
              <div><label className="block text-xs text-gray-400 mb-1">URL del Banner</label>
                <input type="text" value={profileForm.banner_url || ''} onChange={e => setProfileForm({ ...profileForm, banner_url: e.target.value })}
                  className="w-full px-3 py-2 bg-dark-700 border border-surface-border rounded-lg text-white text-sm focus:outline-none focus:border-neon-red" /></div>
              <div><label className="block text-xs text-gray-400 mb-1">URL del Avatar</label>
                <input type="text" value={profileForm.avatar_url || ''} onChange={e => setProfileForm({ ...profileForm, avatar_url: e.target.value })}
                  className="w-full px-3 py-2 bg-dark-700 border border-surface-border rounded-lg text-white text-sm focus:outline-none focus:border-neon-red" /></div>
              <div><label className="block text-xs text-gray-400 mb-1">URL del Canal de YouTube</label>
                <input type="text" value={profileForm.yt_channel_url || ''} onChange={e => setProfileForm({ ...profileForm, yt_channel_url: e.target.value })}
                  className="w-full px-3 py-2 bg-dark-700 border border-surface-border rounded-lg text-white text-sm focus:outline-none focus:border-neon-red" /></div>
              <div className="flex gap-2 pt-2">
                <button onClick={handleSaveProfile} disabled={saving}
                  className="flex-1 flex items-center justify-center gap-1.5 px-4 py-2 bg-neon-red text-white rounded-lg font-bold text-sm hover:bg-neon-red/80 disabled:opacity-50">
                  <Save size={14} /> {saving ? 'Guardando...' : 'Guardar Perfil'}
                </button>
                <button onClick={handleSyncYouTube} className="px-4 py-2 bg-red-600/10 border border-red-600/30 text-red-400 rounded-lg text-sm hover:bg-red-600/20">
                  <Youtube size={14} className="inline mr-1" /> Sync YT
                </button>
                <button onClick={() => setEditingProfile(false)} className="px-4 py-2 bg-dark-600 text-gray-300 rounded-lg text-sm hover:bg-dark-500">Cancelar</button>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* --- Config Viewer Panel --- */}
      {showConfig && channel?.config_json && typeof channel.config_json === 'object' && (
        <div className="glass rounded-xl p-5 mt-6 space-y-4 animate-fade-in">
          <div className="flex items-center justify-between">
            <h3 className="font-display text-lg font-semibold text-white flex items-center gap-2">
              <Settings size={20} className="text-neon-cyan" /> Configuración del Canal
            </h3>
            <div className="flex gap-2">
              {editingConfig ? (
                <>
                  <button onClick={handleSaveConfig} disabled={syncing}
                    className="flex items-center gap-1 px-3 py-1.5 bg-neon-gold text-dark-900 rounded-lg text-xs font-bold hover:bg-neon-gold/80 disabled:opacity-50">
                    <Save size={12} /> {syncing ? 'Guardando...' : 'Guardar'}
                  </button>
                  <button onClick={() => setEditingConfig(false)}
                    className="px-3 py-1.5 bg-dark-600 text-gray-300 rounded-lg text-xs hover:bg-dark-500">Cancelar</button>
                </>
              ) : (
                <>
                  <button onClick={startEditingConfig}
                    className="flex items-center gap-1 px-3 py-1.5 bg-neon-gold/10 border border-neon-gold/30 text-neon-gold rounded-lg text-xs font-medium hover:bg-neon-gold/20">
                    <Edit3 size={12} /> Editar
                  </button>
                  <button onClick={handleSyncConfig} disabled={syncing}
                    className="flex items-center gap-1.5 px-3 py-1.5 bg-neon-cyan text-dark-900 rounded-lg text-xs font-bold hover:bg-neon-cyan/80 disabled:opacity-50">
                    <RefreshCw size={12} className={syncing ? 'animate-spin' : ''} /> {syncing ? 'Sync...' : 'Sync Python'}
                  </button>
                </>
              )}
            </div>
          </div>

          <div className="grid gap-3 md:grid-cols-2">
            {CONFIG_SECTIONS.map((section: ConfigSection) => (
              <div key={section.key} className="bg-dark-700/50 rounded-lg p-3 border border-surface-border">
                <h4 className="text-xs font-semibold text-gray-400 uppercase tracking-wider mb-2">{section.label}</h4>
                <div className="space-y-1.5">
                  {section.fields.map((field: ConfigField) => (
                    <div key={field.key} className="flex items-start justify-between gap-2">
                      <div className="flex items-center gap-1 min-w-0">
                        {field.affectsVideo && <Zap size={10} className="text-neon-gold shrink-0" />}
                        <span className="text-xs text-gray-400 truncate">{field.label}</span>
                      </div>
                      <div className="text-right shrink-0 max-w-[60%] overflow-hidden">
                        {editingConfig 
                          ? renderEditField(field, editConfig[field.key])
                          : renderConfigValue(field, channel.config_json)
                        }
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            ))}
          </div>

          <p className="text-[10px] text-gray-600 flex items-center gap-1">
            <Zap size={10} className="text-neon-gold" /> = afecta a la generación de video.
            {editingConfig ? ' Editando configuración. Guarda los cambios al terminar.' : ' Para editar, haz clic en "Editar". O modifica config.py y haz "Sync Python".'}
          </p>
        </div>
      )}

      {/* --- Auth Modal --- */}
      {showAuthModal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm" onClick={() => setShowAuthModal(false)}>
          <div className="glass rounded-xl p-6 w-full max-w-lg space-y-4 animate-slide-up" onClick={e => e.stopPropagation()}>
            <h3 className="font-display text-lg font-semibold text-white flex items-center gap-2"><Key size={18} className="text-neon-cyan" /> Conectar YouTube</h3>
            <p className="text-sm text-gray-400">
              1. Abre esta URL en tu navegador:<br/>
              <a href={authUrl} target="_blank" rel="noopener noreferrer" 
                 className="text-neon-cyan underline break-all text-xs flex items-center gap-1 mt-1">
                <ExternalLink size={12} /> {authUrl.substring(0, 80)}...
              </a>
            </p>
            <p className="text-sm text-gray-400">2. Autoriza con la cuenta Google del canal</p>
            <p className="text-sm text-gray-400">3. Copia el código de la barra de direcciones (entre <code className="text-neon-gold">code=</code> y <code className="text-neon-gold">&amp;scope=</code>)</p>
            <div>
              <label className="block text-xs text-gray-400 mb-1">Código de autorización</label>
              <input type="text" value={authCode} onChange={e => setAuthCode(e.target.value)}
                placeholder="4/0AanRRr..."
                className="w-full px-3 py-2 bg-dark-700 border border-surface-border rounded-lg text-white text-sm focus:outline-none focus:border-neon-cyan" />
            </div>
            <div className="flex gap-2">
              <button onClick={handleSubmitAuthCode} disabled={authLoading || !authCode.trim()}
                className="flex-1 flex items-center justify-center gap-1.5 px-4 py-2 bg-neon-cyan text-dark-900 rounded-lg font-bold text-sm hover:bg-neon-cyan/80 disabled:opacity-50">
                <Link2 size={14} /> {authLoading ? 'Conectando...' : 'Completar Conexión'}
              </button>
              <button onClick={() => setShowAuthModal(false)} className="px-4 py-2 bg-dark-600 text-gray-300 rounded-lg text-sm hover:bg-dark-500">Cancelar</button>
            </div>
          </div>
        </div>
      )}

      {/* --- Manual Setup Modal --- */}
      {showManualSetup && manualSetup && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm" onClick={() => setShowManualSetup(false)}>
          <div className="glass rounded-xl p-6 w-full max-w-lg space-y-4 animate-slide-up max-h-[85vh] overflow-y-auto" onClick={e => e.stopPropagation()}>
            <h3 className="font-display text-lg font-semibold text-white flex items-center gap-2"><Clipboard size={18} className="text-neon-gold" /> Configuración Manual</h3>
            <p className="text-xs text-gray-500">Estos campos NO se pueden subir por API. Debes configurarlos en YouTube Studio.</p>
            
            <div className="space-y-2">
              <p className="text-sm font-medium text-neon-gold">Nombre sugerido del canal:</p>
              <p className="text-lg text-white font-display">{manualSetup.channel_name_suggested || '—'}</p>
            </div>

            {(manualSetup.manual_fields || []).map((f: any) => (
              <div key={f.field} className="bg-dark-700/50 rounded-lg p-3 border border-surface-border">
                <div className="flex items-center justify-between mb-1">
                  <span className="text-sm font-medium text-white capitalize">{f.field.replace('_', ' ')}</span>
                  {f.ready && <span className="text-xs text-green-400">✅ Listo</span>}
                  {!f.ready && <span className="text-xs text-neon-gold">📁 Pendiente</span>}
                </div>
                <p className="text-xs text-gray-500 mb-1">{f.reason}</p>
                {f.file && (
                  <a href={f.file.startsWith('/') ? f.file : f.file} download target="_blank" rel="noopener noreferrer"
                    className="text-xs text-neon-cyan hover:underline flex items-center gap-1">
                    <ExternalLink size={12} /> Descargar ({f.dimensions || 'archivo'})
                  </a>
                )}
                {f.suggested_value && <p className="text-xs text-gray-400 mt-1">Valor: {f.suggested_value}</p>}
              </div>
            ))}

            <div>
              <p className="text-sm font-medium text-white mb-2">Instrucciones:</p>
              <ol className="text-xs text-gray-400 space-y-1 list-decimal list-inside">
                {(manualSetup.instructions || []).map((inst: string, i: number) => (
                  <li key={i}>{inst}</li>
                ))}
              </ol>
            </div>

            {manualSetup.copy_paste_data && (
              <div>
                <p className="text-sm font-medium text-white mb-1">Datos para copiar/pegar:</p>
                <div className="space-y-2">
                  <div>
                    <p className="text-xs text-gray-500 mb-0.5">Descripción:</p>
                    <pre className="text-xs text-gray-300 bg-dark-800 p-2 rounded whitespace-pre-wrap max-h-24 overflow-y-auto">{manualSetup.copy_paste_data.description?.substring(0, 500)}</pre>
                  </div>
                  <div>
                    <p className="text-xs text-gray-500 mb-0.5">Keywords:</p>
                    <pre className="text-xs text-gray-300 bg-dark-800 p-2 rounded whitespace-pre-wrap max-h-16 overflow-y-auto">{manualSetup.copy_paste_data.keywords?.substring(0, 300)}</pre>
                  </div>
                </div>
              </div>
            )}

            <button onClick={() => setShowManualSetup(false)} className="w-full py-2 bg-dark-600 text-gray-300 rounded-lg text-sm hover:bg-dark-500">Cerrar</button>
          </div>
        </div>
      )}
    </div>
  )
}
