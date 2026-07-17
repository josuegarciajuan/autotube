import { useState, useEffect } from 'react'
import { useParams, Link, useNavigate } from 'react-router-dom'
import { api, formatDate, formatDateTime, formatDuration, formatShortNumber, formatCountdown, formatTargetTime, apiUrl, statusBadge, statusLabel } from '../lib/api'
import { useGeneration } from '../context/GenerationContext'
import { useGenerationProgress } from '../hooks/useWebSocket'
import { ArrowLeft, Wand2, Upload, Play, AlertCircle, Calendar, Youtube, Edit3, Save, Users, Video, Image, Settings, RefreshCw, Zap, Loader2, Key, Link2, Clipboard, ExternalLink, Trash2, Eye, Clock, Plus, Heart, TrendingUp, DollarSign, Award, BarChart3, ListPlus, MessageCircle, Sparkles, Megaphone, Scissors, X, Download, AlertTriangle, Globe, MapPin } from 'lucide-react'
import VideoTiming from '../components/VideoTiming'
import VoiceSelector from '../components/VoiceSelector'
import PublicationModeToggle from '../components/PublicationModeToggle'
import WatchTimeChart from '../components/WatchTimeChart'
import HorariosTab from '../components/HorariosTab'
import { CONFIG_SECTIONS, type ConfigSection, type ConfigField, LIFECYCLE_ACTION_LABELS, LIFECYCLE_STATUS_LABELS, type LifecycleActionType } from '../types/channel'

// ── PromotionTab (inline component) ─────────────────────────

function PromotionTab({ channelId, videos, playlists, setPlaylists, loadingPlaylists,
  setLoadingPlaylists, syncingPlaylists, setSyncingPlaylists, lifecycleActions,
  setLifecycleActions, loadingLifecycle, setLoadingLifecycle, selectedVideoId,
  setSelectedVideoId, promoActionResult, setPromoActionResult }: any) {

  const loadPlaylists = async () => {
    setLoadingPlaylists(true)
    try {
      const data = await api.getChannelPlaylists(channelId)
      setPlaylists(data)
    } catch (e) { console.error('Failed to load playlists', e) }
    setLoadingPlaylists(false)
  }

  const loadLifecycle = async () => {
    setLoadingLifecycle(true)
    try {
      const data = await api.getChannelLifecycleRecent(channelId, 30)
      setLifecycleActions(data)
    } catch (e) { console.error('Failed to load lifecycle', e) }
    setLoadingLifecycle(false)
  }

  useEffect(() => { loadPlaylists(); loadLifecycle() }, [channelId])

  const handleSyncPlaylists = async () => {
    setSyncingPlaylists(true)
    try {
      const result = await api.syncChannelPlaylists(channelId)
      setPromoActionResult(`✅ Playlists: ${result.created?.length || 0} creadas, ${result.existing?.length || 0} existentes`)
      loadPlaylists()
    } catch (e: any) { setPromoActionResult(`❌ Error: ${e.message}`) }
    setSyncingPlaylists(false)
  }

  const handlePromoAction = async (actionType: string, videoId?: number) => {
    const vid = videoId || selectedVideoId
    if (!vid) { setPromoActionResult('❌ Selecciona un video primero'); return }
    setPromoActionResult(null)
    try {
      let result: any
      switch (actionType) {
        case 'playlist_add': result = await api.addVideoToPlaylists(vid); break
        case 'first_comment': result = await api.postFirstComment(vid); break
        case 'comment_reply_1':
        case 'comment_reply_2': result = await api.replyToComments(vid); break
        case 'metadata_reoptimize': result = await api.reoptimizeMetadata(vid); break
        default: result = await api.triggerLifecycleAction(vid, actionType)
      }
      const msg = result.success ? '✅ Ejecutado correctamente' :
        result.skipped ? `⏭️ ${result.reason || 'Omitido'}` :
        result.replied_to !== undefined ? `💬 Respondido: ${result.replied_to}, omitidos: ${result.skipped}` :
        result.added_to ? `📋 Añadido a: ${result.added_to.join(', ')}` :
        result.error ? `❌ ${result.error}` : '✅ Completado'
      setPromoActionResult(msg)
      loadLifecycle()
      if (actionType === 'playlist_add') loadPlaylists()
    } catch (e: any) { setPromoActionResult(`❌ Error: ${e.message}`) }
  }

  const uploadedVideos = videos.filter((v: any) => v.yt_video_id)

  return (
    <div className="space-y-4">
      {/* Playlists */}
      <div className="glass rounded-xl p-4">
        <div className="flex items-center justify-between mb-3">
          <h3 className="text-sm font-semibold text-white flex items-center gap-2">
            <ListPlus size={16} /> Playlists del Canal
          </h3>
          <button onClick={handleSyncPlaylists} disabled={syncingPlaylists}
            className="px-3 py-1.5 bg-neon-red/10 border border-neon-red/30 text-neon-red rounded-lg text-xs hover:bg-neon-red/20 disabled:opacity-50 flex items-center gap-1">
            <RefreshCw size={12} className={syncingPlaylists ? 'animate-spin' : ''} />
            {syncingPlaylists ? 'Sincronizando...' : 'Sincronizar'}
          </button>
        </div>
        {loadingPlaylists ? <RefreshCw size={20} className="animate-spin mx-auto text-gray-500 my-4" /> :
        playlists.length === 0 ? <p className="text-gray-500 text-xs">Sin playlists. Haz clic en Sincronizar.</p> :
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead><tr className="text-gray-500 border-b border-dark-600">
              <th className="text-left py-2">Nombre</th><th className="text-left py-2">Tipo</th><th className="text-left py-2">YouTube ID</th>
            </tr></thead>
            <tbody>{playlists.map((pl: any) => (
              <tr key={pl.id} className="border-b border-dark-600/50">
                <td className="py-2 text-white">{pl.name || pl.slug}</td>
                <td className="py-2">
                  <span className={`px-1.5 py-0.5 rounded text-[10px] ${
                    pl.playlist_type === 'main' ? 'bg-blue-600/20 text-blue-400' :
                    pl.playlist_type === 'onboarding' ? 'bg-green-600/20 text-green-400' : 'bg-dark-600 text-gray-400'
                  }`}>{pl.playlist_type}</span>
                </td>
                <td className="py-2 text-gray-500 font-mono text-[10px]">{pl.yt_playlist_id?.slice(0, 20)}...</td>
              </tr>
            ))}</tbody>
          </table>
        </div>}
      </div>

      {/* Lifecycle Timeline */}
      <div className="glass rounded-xl p-4">
        <div className="flex items-center justify-between mb-3">
          <h3 className="text-sm font-semibold text-white flex items-center gap-2">
            <Clock size={16} /> Timeline de Promoción
          </h3>
          <button onClick={loadLifecycle} className="text-gray-400 hover:text-white">
            <RefreshCw size={14} className={loadingLifecycle ? 'animate-spin' : ''} />
          </button>
        </div>
        {loadingLifecycle ? <RefreshCw size={20} className="animate-spin mx-auto text-gray-500 my-4" /> :
        lifecycleActions.length === 0 ? <p className="text-gray-500 text-xs">No hay acciones registradas aún.</p> :
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead><tr className="text-gray-500 border-b border-dark-600">
              <th className="text-left py-2">Video</th><th className="text-left py-2">Acción</th>
              <th className="text-left py-2 hidden sm:table-cell">Programado</th><th className="text-left py-2">Estado</th>
              <th className="text-left py-2">Acción</th>
            </tr></thead>
            <tbody>{lifecycleActions.slice(0, 20).map((a: any) => (
              <tr key={a.id} className="border-b border-dark-600/50">
                <td className="py-2 text-white max-w-[120px] truncate">{a.video_title || `#${a.video_id}`}</td>
                <td className="py-2 text-gray-300">{LIFECYCLE_ACTION_LABELS[a.action_type as LifecycleActionType] || a.action_type}</td>
                <td className="py-2 text-gray-500 hidden sm:table-cell">{a.scheduled_for ? formatDateTime(a.scheduled_for) : '-'}</td>
                <td className="py-2">{LIFECYCLE_STATUS_LABELS[a.status] || a.status}</td>
                <td className="py-2">
                  {a.status === 'pending' && (
                    <button onClick={() => handlePromoAction(a.action_type, a.video_id)}
                      className="text-neon-red hover:underline text-[10px]">Ejecutar</button>
                  )}
                </td>
              </tr>
            ))}</tbody>
          </table>
        </div>}
      </div>

      {/* Manual Actions */}
      <div className="glass rounded-xl p-4">
        <h3 className="text-sm font-semibold text-white flex items-center gap-2 mb-3">
          <Sparkles size={16} /> Acciones Manuales
        </h3>
        <div className="flex flex-wrap items-center gap-3 mb-3">
          <span className="text-xs text-gray-400">Video:</span>
          <select value={selectedVideoId || ''} onChange={(e) => setSelectedVideoId(e.target.value ? Number(e.target.value) : null)}
            className="bg-dark-800 border border-dark-600 rounded-lg px-3 py-1.5 text-sm text-white">
            <option value="">Seleccionar video...</option>
            {uploadedVideos.map((v: any) => (
              <option key={v.id} value={v.id}>{v.titulo_final || `Video #${v.id}`}</option>
            ))}
          </select>
        </div>
        {promoActionResult && (
          <div className={`mb-3 p-2 rounded-lg text-xs ${
            promoActionResult.startsWith('✅') ? 'bg-green-600/10 border border-green-600/30 text-green-400' :
            promoActionResult.startsWith('❌') ? 'bg-red-600/10 border border-red-600/30 text-red-400' :
            'bg-blue-600/10 border border-blue-600/30 text-blue-400'
          }`}>{promoActionResult}</div>
        )}
        <div className="flex flex-wrap gap-2">
          <button onClick={() => handlePromoAction('playlist_add')}
            className="px-3 py-2 bg-dark-700 border border-dark-600 text-gray-300 rounded-lg text-xs hover:bg-dark-600 flex items-center gap-1">
            <ListPlus size={12} /> Añadir a playlists
          </button>
          <button onClick={() => handlePromoAction('first_comment')}
            className="px-3 py-2 bg-dark-700 border border-dark-600 text-gray-300 rounded-lg text-xs hover:bg-dark-600 flex items-center gap-1">
            <MessageCircle size={12} /> Postear primer comentario
          </button>
          <button onClick={() => handlePromoAction('comment_reply_1')}
            className="px-3 py-2 bg-dark-700 border border-dark-600 text-gray-300 rounded-lg text-xs hover:bg-dark-600 flex items-center gap-1">
            <MessageCircle size={12} /> Responder comentarios
          </button>
          <button onClick={() => handlePromoAction('metadata_reoptimize')}
            className="px-3 py-2 bg-dark-700 border border-dark-600 text-gray-300 rounded-lg text-xs hover:bg-dark-600 flex items-center gap-1">
            <Sparkles size={12} /> Reoptimizar metadata
          </button>
        </div>
      </div>
    </div>
  )
}

export default function ChannelDetail() {
  const { id } = useParams<{ id: string }>()
  const channelId = Number(id)
  const navigate = useNavigate()

  const [channel, setChannel] = useState<any>(null)
  const [videos, setVideos] = useState<any[]>([])

  const [filterPlaylistId, setFilterPlaylistId] = useState<number | null>(null)
  const [channelPlaylists, setChannelPlaylists] = useState<any[]>([])
  const [loading, setLoading] = useState(true)
  const [generating, setGenerating] = useState(false)
  const [editingProfile, setEditingProfile] = useState(false)
  const [profileForm, setProfileForm] = useState({ name: '', description: '', banner_url: '', avatar_url: '', yt_channel_url: '', google_account: '', yt_studio_url: '' })
  const [saving, setSaving] = useState(false)
  const [videoStats, setVideoStats] = useState<Record<string, any>>({})
  const [shortStats, setShortStats] = useState<Record<string, any>>({})
  const [channelYtStats, setChannelYtStats] = useState<any>(null)
  const [channelShortsStats, setChannelShortsStats] = useState<any>(null)
  const [channelVideosAggregate, setChannelVideosAggregate] = useState<any>(null)
  const [showConfig, setShowConfig] = useState(false)
  const [syncing, setSyncing] = useState(false)
  const [refreshingStats, setRefreshingStats] = useState(false)
  const [statsRefreshMsg, setStatsRefreshMsg] = useState<string | null>(null)
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

  // Templates state
  const [templates, setTemplates] = useState<Record<string, any> | null>(null)
  const [regeneratingTemplate, setRegeneratingTemplate] = useState<string | null>(null)
  const [templateResult, setTemplateResult] = useState<{ ok: boolean; message: string } | null>(null)

  // Delete confirmation state
  const [deleteTarget, setDeleteTarget] = useState<number | null>(null)

  // Upload confirmation modal (before generation)
  const [showUploadConfirm, setShowUploadConfirm] = useState(false)
  const [showSourceModeModal, setShowSourceModeModal] = useState(false)
  const [sourceMode, setSourceMode] = useState<'original' | 'viral'>('original')
  
  // Short creation state (per video)
  const [creatingShort, setCreatingShort] = useState<number | null>(null)
  const [shortResult, setShortResult] = useState<{ ok: boolean; message: string; url?: string } | null>(null)
  
  // Native short generation from Shorts tab
  const [generatingNativeShort, setGeneratingNativeShort] = useState(false)
  const [nativeShortResult, setNativeShortResult] = useState<{ ok: boolean; message: string; url?: string } | null>(null)
  
  // Tab state for Videos/Shorts/Live/Growth/Promotion/Slots
  const [videoTab, setVideoTab] = useState<'videos' | 'shorts' | 'live' | 'growth' | 'promotion' | 'slots'>('videos')
  const [shorts, setShorts] = useState<any[]>([])
  const [loadingShorts, setLoadingShorts] = useState(false)

  // Growth tab state
  const [growthData, setGrowthData] = useState<any>(null)
  const [monetizationData, setMonetizationData] = useState<any>(null)
  const [watchTimeData, setWatchTimeData] = useState<any>(null)
  const [milestonesData, setMilestonesData] = useState<any>(null)
  const [contentRanking, setContentRanking] = useState<any[]>([])
  const [growthDays, setGrowthDays] = useState(30)

  // Scheduled publishing mode
  const [scheduledMode, setScheduledMode] = useState('immediate')

  // Promotion tab state
  const [playlists, setPlaylists] = useState<any[]>([])
  const [loadingPlaylists, setLoadingPlaylists] = useState(false)
  const [syncingPlaylists, setSyncingPlaylists] = useState(false)
  const [lifecycleActions, setLifecycleActions] = useState<any[]>([])
  const [loadingLifecycle, setLoadingLifecycle] = useState(false)
  const [selectedVideoId, setSelectedVideoId] = useState<number | null>(null)
  const [promoActionResult, setPromoActionResult] = useState<string | null>(null)

  const { addJob, removeJob, activeJobs, isChannelBusy } = useGeneration()
  // Find active job for THIS channel (for inline progress display)
  const channelActiveJob = activeJobs.find(j => j.channelId === channelId)
  const { progress } = useGenerationProgress(channelActiveJob?.jobId ?? null)

  // Block generation if ANY job is active for this channel
  const busy = generating || isChannelBusy(channelId)

  useEffect(() => {
    async function load() {
      try {
        const [ch, vids] = await Promise.all([
          api.getChannel(channelId),
          api.getChannelVideos(channelId),
        ])
        setChannel(ch)
        setVideos(vids)
        // Load playlists for filter
        try { const pl = await api.getChannelPlaylists(channelId); setChannelPlaylists(pl) } catch {}
        // Parse scheduled mode from config_json
        try {
          const cfg = typeof ch.config_json === 'string' ? JSON.parse(ch.config_json) : (ch.config_json || {})
          setScheduledMode(cfg.PUBLISH_MODE || 'immediate')
        } catch { setScheduledMode('immediate') }
        setProfileForm({ name: ch.name || '', description: ch.description || '', banner_url: ch.banner_url || '', avatar_url: ch.avatar_url || '', yt_channel_url: ch.yt_channel_url || '', google_account: ch.google_account || '', yt_studio_url: ch.yt_studio_url || '' })
      } catch (e) { console.error(e) }
      setLoading(false)
    }
    load()
  }, [channelId])

  // Restore generating state from context on mount / when activeJobs change
  useEffect(() => {
    if (channelActiveJob && channelActiveJob.jobId) {
      setGenerating(true)
      pollForCompletion(channelActiveJob.jobId)
    }
  }, [channelActiveJob?.jobId, channelId])

  // Reload videos when filter changes
  useEffect(() => {
    if (!channelId) return
    api.getChannelVideos(channelId, undefined, filterPlaylistId || undefined)
      .then(setVideos).catch(() => {})
  }, [filterPlaylistId, channelId])
  useEffect(() => {
    api.getAuthStatus(channelId).then(setAuthStatus).catch(() => {})
  }, [channelId])

  // Fetch templates
  useEffect(() => {
    if (channel?.id) {
      api.getTemplates(channel.id).then(setTemplates).catch(() => {})
    }
  }, [channel?.id])

  // Fetch YouTube stats for uploaded videos
  useEffect(() => {
    const ytIds = videos.filter((v: any) => v.yt_video_id).map((v: any) => v.yt_video_id)
    if (ytIds.length === 0) return
    api.getVideoStats(ytIds).then(stats => setVideoStats(stats)).catch(() => {})
  }, [videos])

  // Fetch channel-level YouTube stats (total views, watch hours)
  useEffect(() => {
    if (!channel?.id) return
    api.getChannelYoutubeStats(channel.id).then(res => {
      if (res?.ok && res.stats) setChannelYtStats(res.stats)
    }).catch(() => {})
  }, [channel?.id])

  // Fetch channel shorts aggregate stats
  useEffect(() => {
    if (!channel?.id) return
    api.getChannelShortsStats(channel.id).then(res => {
      if (res?.ok && res.shorts_stats) setChannelShortsStats(res.shorts_stats)
    }).catch(() => {})
  }, [channel?.id])

  // Fetch long-form videos aggregate stats (vistas/likes por vídeos largos)
  useEffect(() => {
    if (!channel?.id) return
    api.getChannelVideosAggregate(channel.id).then(res => {
      if (res?.ok && res.videos_stats) setChannelVideosAggregate(res.videos_stats)
    }).catch(() => {})
  }, [channel?.id])

  // Poll videos list only after generation completes (handled by pollForCompletion below)
  // WebSocket provides real-time progress during generation — no need for separate polling

  // Load shorts when tab switches
  useEffect(() => {
    if (videoTab !== 'shorts' || !channelId) return
    async function loadShorts() {
      setLoadingShorts(true)
      try {
        const data = await fetch(`/api/shorts?channel_id=${channelId}&limit=50`).then(r => r.json())
        setShorts(data)
      } catch {}
      setLoadingShorts(false)
    }
    loadShorts()
  }, [videoTab, channelId])

  // Load growth tab data when switched
  useEffect(() => {
    if (videoTab !== 'growth' || !channelId) return
    async function loadGrowth() {
      try {
        const [growth, mon, milestones, content, watchtime] = await Promise.all([
          api.getChannelGrowth(channelId, growthDays),
          api.getChannelMonetization(channelId),
          api.getChannelMilestones(channelId),
          api.getChannelContentRanking(channelId, 'views', 15),
          api.getChannelWatchTime(channelId),
        ])
        setGrowthData(growth)
        setMonetizationData(mon)
        setMilestonesData(milestones)
        setContentRanking(content?.videos || [])
        setWatchTimeData(watchtime)
      } catch (e) {
        console.error('Error loading growth data:', e)
      }
    }
    loadGrowth()
  }, [videoTab, channelId, growthDays])
  useEffect(() => {
    const ytIds = shorts.filter((s: any) => s.youtube_id).map((s: any) => s.youtube_id)
    if (ytIds.length === 0) return
    api.getVideoStats(ytIds).then(stats => setShortStats(stats)).catch(() => {})
  }, [shorts])

  async function handleGenerate(upload: boolean = true, mode: 'original' | 'viral' = 'original') {
    setGenerating(true)
    setShowUploadConfirm(false)
    setShowSourceModeModal(false)
    try {
      const isTestChannel = channel?.slug === 'test'
      const result = await api.generateVideo({
        channel_id: channelId,
        action: isTestChannel ? 'generate_and_upload' : 'generate_and_upload',
        test_mode: isTestChannel,
        upload,
        source_mode: mode,
      })
      addJob({
        jobId: result.job_id,
        channelId,
        channelName: channel?.name || 'Canal',
        action: 'generate_and_upload',
        videoId: result.video_id,
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
          removeJob(jobId)
          const vids = await api.getChannelVideos(channelId)
          setVideos(vids)
        } else {
          setTimeout(check, 5000)
        }
      } catch {
        setTimeout(check, 5000)
      }
    }
    setTimeout(check, 5000)
  }

  async function handleCreateShort(videoId: number) {
    setCreatingShort(videoId)
    setShortResult(null)
    const jobId = -(videoId * 1000 + Date.now() % 1000) // negative ID for shorts
    addJob({ jobId, channelId, channelName: channel?.name || '', action: 'Creando Short', videoId })
    try {
      const res = await fetch(`/api/shorts/extract-and-publish/${videoId}`, { method: 'POST' })
      const data = await res.json()
      if (!res.ok) throw new Error(data.detail || `Error ${res.status}`)
      setShortResult({ ok: true, message: `¡Short publicado! "${data.title || 'Sin título'}"`, url: data.youtube_url })
    } catch (e: any) {
      setShortResult({ ok: false, message: `Error: ${e.message}` })
    }
    setCreatingShort(null)
    removeJob(jobId)
    // Toast stays until dismissed manually (no auto-hide)
  }

  async function handleGenerateNativeShort() {
    setGeneratingNativeShort(true)
    setNativeShortResult(null)
    const jobId = -(channelId * 20000 + Date.now() % 10000)
    addJob({ jobId, channelId, channelName: channel?.name || '', action: 'Generando Short Nativo' })
    try {
      const res = await fetch(`/api/shorts/generate-native/${channelId}`, { method: 'POST' })
      const data = await res.json()
      if (!res.ok) throw new Error(data.detail || 'Error')
      setNativeShortResult({ ok: true, message: `¡Short publicado! "${data.title?.slice(0, 35)}..."`, url: data.youtube_url })
      // Reload shorts
      const sdata = await fetch(`/api/shorts?channel_id=${channelId}&limit=50`).then(r => r.json())
      setShorts(sdata)
    } catch (e: any) {
      setNativeShortResult({ ok: false, message: e.message })
    }
    setGeneratingNativeShort(false)
    removeJob(jobId)
  }

  async function handleUpload(videoId: number) {
    try { await api.uploadVideo(videoId); alert('Subida iniciada'); const vids = await api.getChannelVideos(channelId); setVideos(vids) } catch (e: any) { alert('Error: ' + e.message) }
  }

  async function handleDownloadThumbnail(videoId: number) {
    try {
      const res = await fetch(apiUrl(`/thumbnail/${videoId}`))
      const blob = await res.blob()
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `miniatura-${videoId}.jpg`
      a.click()
      URL.revokeObjectURL(url)
    } catch (e: any) { alert('Error al descargar: ' + e.message) }
  }

  async function handleDelete(videoId: number) {
    try {
      await api.deleteVideo(videoId)
      const vids = await api.getChannelVideos(channelId)
      setVideos(vids)
      setDeleteTarget(null)
    } catch (e: any) { alert('Error: ' + e.message) }
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

  async function handleRefreshStats() {
    setRefreshingStats(true)
    setStatsRefreshMsg(null)
    try {
      const result = await api.collectChannelStats(channelId)
      setStatsRefreshMsg(`${result.videos_updated} videos · ${result.shorts_updated} shorts · canal=${result.channel_updated ? 'OK' : 'no actualizado'}`)
      // Refresh channel stats display
      try {
        const ytStats = await api.getChannelYoutubeStats(channelId)
        if (ytStats?.ok && ytStats.stats) setChannelYtStats(ytStats.stats)
      } catch {}
      try {
        const shortsStats = await api.getChannelShortsStats(channelId)
        if (shortsStats?.ok && shortsStats.shorts_stats) setChannelShortsStats(shortsStats.shorts_stats)
      } catch {}
      try {
        const aggStats = await api.getChannelVideosAggregate(channelId)
        if (aggStats?.ok && aggStats.videos_stats) setChannelVideosAggregate(aggStats.videos_stats)
      } catch {}
    } catch (e: any) {
      setStatsRefreshMsg(`Error: ${e.message || 'desconocido'}`)
    } finally {
      setRefreshingStats(false)
    }
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
    if (field.type === 'voice-select') {
      return <VoiceSelector config={editConfig} onUpdateField={updateConfigField} />
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

  async function handleRegenerateTemplate(segment: string) {
    if (!channel?.id) return
    setRegeneratingTemplate(segment)
    try {
      await api.generateTemplate(channel.id, segment)
      const updated = await api.getTemplates(channel.id)
      setTemplates(updated)
      setTemplateResult({ ok: true, message: `El template "${segment}" se regeneró con éxito.` })
    } catch (e: any) {
      setTemplateResult({ ok: false, message: e.message || 'Error al generar el template' })
    } finally {
      setRegeneratingTemplate(null)
    }
  }

  function renderConfigValue(field: ConfigField, config: Record<string, any>): React.ReactNode {
    const value = config[field.key]
    if (field.type === 'voice-select') {
      const engine = config.TTS_ENGINE === 'kokoro' ? 'kokoro' : 'edgetts'
      const voiceName = engine === 'kokoro'
        ? (config.KOKORO_VOICE || 'em_santa')
        : (config.VOICE_ID || 'es-MX-JorgeNeural')
      return (
        <span className="text-xs text-neon-cyan">
          {voiceName} <span className="text-[10px] text-gray-500">({engine === 'kokoro' ? 'Kokoro' : 'Edge-TTS'})</span>
        </span>
      )
    }
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
        <div className="w-full h-32 sm:h-40 md:h-52 rounded-xl overflow-hidden bg-gradient-to-r from-dark-700 via-dark-600 to-neon-red/20">
          {channel.banner_url ? (
            <img src={channel.banner_url} alt="Banner" className="w-full h-full object-cover" />
          ) : (
            <div className="w-full h-full flex items-center justify-center">
              <Image size={48} className="text-gray-700" />
            </div>
          )}
        </div>
        
        {/* Avatar + Info */}
        <div className="px-4 md:px-6 -mt-8 sm:-mt-10 flex flex-col sm:flex-row items-start sm:items-end gap-3 sm:gap-5">
          <div className="w-16 h-16 sm:w-20 sm:h-20 md:w-24 md:h-24 rounded-full border-4 border-dark-900 bg-dark-700 overflow-hidden shrink-0">
            {channel.avatar_url ? (
              <img src={channel.avatar_url} alt="Avatar" className="w-full h-full object-cover" />
            ) : (
              <div className="w-full h-full flex items-center justify-center bg-neon-red/20">
                <Youtube size={28} className="text-neon-red" />
              </div>
            )}
          </div>
          
          <div className="flex-1 pb-1 min-w-0 w-full sm:w-auto">
            <h1 className="font-display text-lg sm:text-xl md:text-2xl font-bold text-white truncate">{channel.name}</h1>
            <div className="flex items-center gap-2 sm:gap-3 text-[11px] sm:text-xs text-gray-400 mt-0.5 flex-wrap">
              <span className="flex items-center gap-1"><Users size={11} />{uploadedCount} videos</span>
              <span className="flex items-center gap-1"><Video size={11} />{videos.length} total</span>
              <span className="hidden sm:inline">·</span>
              <span className="hidden sm:inline">{channel.slug}</span>
            </div>
            {/* Channel YouTube Stats */}
            {channelYtStats && (
              <div className="flex items-center gap-3 text-[11px] sm:text-xs mt-1 flex-wrap">
                <span className="flex items-center gap-1 text-neon-cyan">
                  <Eye size={11} />
                  <span className="font-mono tabular-nums">{formatShortNumber(channelYtStats.viewCount || '0')}</span> vistas totales
                </span>
                <span className="flex items-center gap-1 text-neon-pink">
                  <Users size={11} />
                  <span className="font-mono tabular-nums">{formatShortNumber(channelYtStats.subscriberCount || '0')}</span> suscriptores
                </span>
                  {channelYtStats.estimatedHoursWatched > 0 && (
                    <span className="flex items-center gap-1 text-neon-gold">
                      <Clock size={11} />
                      <span className="font-mono tabular-nums">{formatShortNumber(channelYtStats.estimatedHoursWatched)}</span> horas
                    </span>
                  )}
                </div>
              )}
              {/* Shorts aggregate stats + total likes */}
              {channelShortsStats && channelShortsStats.total > 0 && (
                <div className="flex items-center gap-3 text-[11px] sm:text-xs mt-0.5 flex-wrap">
                  <span className="flex items-center gap-1 text-neon-purple">
                    <Zap size={11} />
                    <span className="font-mono tabular-nums">{channelShortsStats.published || 0}</span> shorts
                  </span>
                  {channelShortsStats.total_views > 0 && (
                    <span className="flex items-center gap-1 text-neon-cyan">
                      <Eye size={11} />
                      <span className="font-mono tabular-nums">{formatShortNumber(channelShortsStats.total_views)}</span> vistas shorts
                    </span>
                  )}
                  {channelShortsStats.total_likes > 0 && (
                    <span className="flex items-center gap-1 text-neon-pink">
                      <Heart size={11} />
                      <span className="font-mono tabular-nums">{formatShortNumber(channelShortsStats.total_likes)}</span> likes shorts
                    </span>
                  )}
                </div>
              )}
              {/* Long-form videos aggregate + combined total */}
              {channelVideosAggregate && (channelVideosAggregate.video_count > 0 || channelVideosAggregate.total_views > 0) && (
                <div className="flex items-center gap-3 text-[11px] sm:text-xs mt-0.5 flex-wrap">
                  <span className="flex items-center gap-1 text-neon-gold">
                    <Video size={11} />
                    <span className="font-mono tabular-nums">{channelVideosAggregate.video_count || 0}</span> vídeos largos
                  </span>
                  {channelVideosAggregate.total_views > 0 && (
                    <span className="flex items-center gap-1 text-neon-cyan">
                      <Eye size={11} />
                      <span className="font-mono tabular-nums">{formatShortNumber(channelVideosAggregate.total_views)}</span> vistas vídeos
                    </span>
                  )}
                  {channelVideosAggregate.total_likes > 0 && (
                    <span className="flex items-center gap-1 text-neon-pink">
                      <Heart size={11} />
                      <span className="font-mono tabular-nums">{formatShortNumber(channelVideosAggregate.total_likes)}</span> likes vídeos
                    </span>
                  )}
                </div>
              )}
              {/* Combined total: videos DB + shorts DB */}
              {(channelVideosAggregate?.total_views > 0 || channelShortsStats?.total_views > 0) && (
                <div className="flex items-center gap-2 text-[11px] sm:text-xs mt-0.5 pt-0.5 border-t border-surface-border/40">
                  <span className="flex items-center gap-1 text-neon-red font-semibold">
                    <Eye size={12} />
                    <span className="font-mono tabular-nums text-xs sm:text-sm">
                      {formatShortNumber((channelVideosAggregate?.total_views || 0) + (channelShortsStats?.total_views || 0))}
                    </span>
                    vistas combinadas (vídeos + shorts)
                  </span>
                </div>
              )}
              {channel.description && (
                <p className="text-xs text-gray-400 mt-1.5 line-clamp-2">{channel.description}</p>
              )}
            </div>

          <div className="flex gap-1.5 sm:gap-2 pb-1 shrink-0 mt-1 sm:mt-0 w-full sm:w-auto">
            {channel.yt_channel_url && (
              <a href={channel.yt_channel_url} target="_blank" rel="noopener noreferrer"
                className="flex items-center gap-1 px-2.5 sm:px-3 py-1.5 bg-red-600 text-white rounded-full text-xs font-medium hover:bg-red-700 transition-colors">
                <Youtube size={14} /> YouTube
              </a>
            )}
            {channel.yt_studio_url && (
              <a href={channel.yt_studio_url} target="_blank" rel="noopener noreferrer"
                className="flex items-center gap-1 px-2.5 sm:px-3 py-1.5 bg-neon-cyan/10 border border-neon-cyan/30 text-neon-cyan rounded-full text-xs font-medium hover:bg-neon-cyan/20 transition-colors">
                <ExternalLink size={14} /> <span className="hidden sm:inline">YT Studio</span>
              </a>
            )}
            <button onClick={() => setEditingProfile(true)}
              className="flex items-center gap-1 px-2.5 sm:px-3 py-1.5 bg-dark-700 border border-surface-border text-gray-300 rounded-full text-xs hover:bg-dark-600 transition-colors">
              <Edit3 size={12} /> <span className="hidden sm:inline">Editar perfil</span><span className="sm:hidden">Editar</span>
            </button>
            <button onClick={() => setShowConfig(!showConfig)}
              className="flex items-center gap-1 px-2.5 sm:px-3 py-1.5 bg-dark-700 border border-neon-cyan/30 text-neon-cyan rounded-full text-xs hover:bg-dark-600 transition-colors">
              <Settings size={12} /> <span className="hidden sm:inline">{showConfig ? 'Ocultar' : 'Config'}</span><Zap size={12} className="sm:hidden" />
            </button>
          </div>
        </div>
      </div>

      {/* --- Quick actions bar --- */}
      <div className="flex items-center gap-1.5 sm:gap-2 mt-4 mb-6 flex-wrap">
        <Link to="/scheduling"
          className="flex items-center gap-1.5 px-3 py-2 bg-neon-gold/10 border border-neon-gold/30 text-neon-gold rounded-lg text-xs sm:text-sm font-medium hover:bg-neon-gold/20 transition-colors">
          <Calendar size={14} /> Programar
        </Link>
        <button onClick={handleStartAuth} disabled={authLoading}
          className={`flex items-center gap-1.5 px-3 py-2 rounded-lg text-xs sm:text-sm font-medium transition-colors ${
            authStatus?.authenticated 
              ? 'bg-green-600/10 border border-green-600/30 text-green-400' 
              : 'bg-neon-cyan/10 border border-neon-cyan/30 text-neon-cyan hover:bg-neon-cyan/20'
          }`}>
          <Key size={14} /> <span className="hidden sm:inline">{authStatus?.authenticated ? 'Conectado' : 'Conectar YouTube'}</span><span className="sm:hidden">{authStatus?.authenticated ? 'YT ✓' : 'Conectar'}</span>
        </button>
        <button onClick={handleSyncYouTube} disabled={syncing}
          className="flex items-center gap-1.5 px-3 py-2 bg-red-600/10 border border-red-600/30 text-red-400 rounded-lg text-xs sm:text-sm font-medium hover:bg-red-600/20 transition-colors">
          <RefreshCw size={14} className={syncing ? 'animate-spin' : ''} /> <span className="hidden sm:inline">{syncing ? 'Syncing...' : 'Sync YouTube'}</span><span className="sm:hidden">Sync</span>
        </button>
        <button onClick={handleRefreshStats} disabled={refreshingStats}
          className="flex items-center gap-1.5 px-3 py-2 bg-cyan-500/10 border border-cyan-500/20 text-cyan-400 rounded-lg text-xs sm:text-sm font-medium hover:bg-cyan-500/20 transition-colors disabled:opacity-50"
          title="Refrescar estadisticas de YouTube para este canal">
          {refreshingStats ? <Loader2 size={14} className="animate-spin" /> : <BarChart3 size={14} />}
          <span className="hidden sm:inline">{refreshingStats ? 'Refrescando...' : 'Refresh Stats'}</span><span className="sm:hidden">Stats</span>
        </button>
        {statsRefreshMsg && (
          <span className={`text-xs ${statsRefreshMsg.startsWith('Error') ? 'text-red-400' : 'text-green-400'} animate-fade-in`}>
            {statsRefreshMsg}
          </span>
        )}
        <button onClick={handleGetManualSetup}
          className="flex items-center gap-1.5 px-3 py-2 bg-dark-600 border border-surface-border text-gray-400 rounded-lg text-xs sm:text-sm font-medium hover:bg-dark-500 transition-colors">
          <Clipboard size={14} /> <span className="hidden sm:inline">Setup Manual</span><span className="sm:hidden">Setup</span>
        </button>
        <PublicationModeToggle channelId={channelId} currentMode={scheduledMode} onToggle={setScheduledMode} />
        <Link to="/channels"
          className="flex items-center gap-1.5 px-3 py-2 text-xs sm:text-sm text-gray-400 hover:text-white transition-colors">
          <ArrowLeft size={14} /> <span className="hidden sm:inline">Canales</span>
        </Link>
      </div>

      {/* --- Generation Panel --- */}
      {videoTab !== 'live' && videoTab !== 'slots' && (
      <div className="glass rounded-xl p-5 space-y-4 mb-6">
        <h3 className="font-display text-lg font-semibold text-white flex items-center gap-2">
          <Wand2 size={20} className="text-neon-gold" /> {videoTab === 'shorts' ? 'Generar Short' : 'Generar Video'}
        </h3>
        {(videoTab === 'shorts' ? generatingNativeShort : busy) ? (
          <div className="flex items-center gap-3 p-4 bg-dark-700/50 rounded-lg">
            <Loader2 size={20} className="text-neon-gold animate-spin" />
            <div className="flex-1 min-w-0">
              <p className="text-sm font-medium text-white">
                {videoTab === 'shorts'
                  ? 'Generando y publicando Short...'
                  : (progress?.message || 'Generando y subiendo video...')}
              </p>
              {videoTab !== 'shorts' && (
                <>
                  <div className="flex items-center gap-2 mt-1">
                    <div className="flex-1 h-1.5 bg-dark-900 rounded-full overflow-hidden">
                      <div className="h-full bg-gradient-to-r from-neon-red to-neon-gold rounded-full transition-all duration-500"
                           style={{ width: `${progress?.progress || 0}%` }} />
                    </div>
                    <span className="text-xs text-neon-red font-mono tabular-nums">
                      {progress?.progress || 0}%
                    </span>
                  </div>
                  {progress?.phase && (
                    <p className="text-xs text-neon-cyan mt-0.5 capitalize">{progress.phase}</p>
                  )}
                  {progress?.detail && (
                    <p className="text-xs text-slate-400 mt-0.5">{progress.detail}</p>
                  )}
                  {progress?.current !== undefined && progress?.total !== undefined && (
                    <p className="text-xs text-slate-500">
                      {progress.current}/{progress.total}
                    </p>
                  )}
                </>
              )}
            </div>
          </div>
        ) : (
          <button
            onClick={videoTab === 'shorts' ? handleGenerateNativeShort : () => setShowSourceModeModal(true)}
            disabled={videoTab === 'shorts' ? generatingNativeShort : busy}
            className="w-full py-4 bg-gradient-to-r from-neon-red to-red-600 text-white rounded-xl font-display font-semibold text-lg hover:shadow-lg hover:shadow-neon-red/20 transition-all duration-300 disabled:opacity-50 flex items-center justify-center gap-3">
            <Wand2 size={22} /> {videoTab === 'shorts' ? 'Generar Short' : channel?.slug === 'test' ? 'Generar video de pruebas' : 'Generar Video'}
          </button>
        )}
        <p className="text-xs text-gray-500 text-center">
          {videoTab === 'shorts'
            ? 'Genera un Short nativo con IA y lo publica automáticamente en YouTube.'
            : 'Genera y sube automáticamente a YouTube. Recibirás una notificación al terminar.'}
        </p>
        {videoTab === 'shorts' && nativeShortResult && (
          <div className={`p-3 rounded-lg text-sm flex items-center gap-2 ${
            nativeShortResult.ok ? 'bg-green-600/10 border border-green-600/30 text-green-400' : 'bg-red-600/10 border border-red-600/30 text-red-400'
          }`}>
            <span>{nativeShortResult.ok ? '✅' : '❌'}</span>
            <span>{nativeShortResult.message}</span>
            {nativeShortResult.url && (
              <a href={nativeShortResult.url} target="_blank" rel="noopener noreferrer"
                 className="ml-auto text-neon-red underline text-xs">Ver en YouTube →</a>
            )}
          </div>
        )}
      </div>
      )}

      {/* --- Templates Panel --- */}
      <div className="glass rounded-xl p-5 space-y-4 mb-6">
        <h3 className="text-lg font-semibold">🎬 Templates del Canal</h3>
        <p className="text-sm text-slate-400">Genera los mini-videos de intro, CTA y outro para tus videos.</p>
        
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-3 sm:gap-4">
          {['intro', 'cta', 'outro'].map(segment => {
            const data = templates?.[segment];
            return (
              <div key={segment} className="bg-white/5 rounded-lg p-3 text-center space-y-2 flex flex-col">
                <div className="text-sm font-medium capitalize">{segment}</div>
                {data ? (
                  <>
                    <video
                      controls
                      muted
                      preload="metadata"
                      poster=""
                      className="w-full rounded-md aspect-video bg-black object-cover"
                      src={apiUrl(`/channels/${channel.id}/templates/${segment}/file?v=${data.generated_at || '0'}`)}
                    />
                    <div className="text-xs text-green-400">✅ Generado</div>
                    {data.generated_at && (
                      <div className="text-[10px] text-gray-500">
                        {new Date(data.generated_at).toLocaleString('es-ES', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit', second: '2-digit' })}
                      </div>
                    )}
                  </>
                ) : (
                  <div className="flex-1 flex items-center justify-center">
                    <div className="text-xs text-yellow-400">⚠️ No generado</div>
                  </div>
                )}
                <button
                  onClick={() => handleRegenerateTemplate(segment)}
                  disabled={regeneratingTemplate === segment}
                  className="w-full px-3 py-1.5 text-xs bg-red-600 hover:bg-red-700 rounded-lg transition disabled:opacity-50 mt-auto"
                >
                  {regeneratingTemplate === segment ? 'Generando...' : 'Regenerar'}
                </button>
              </div>
            );
          })}
        </div>
      </div>

      {/* --- Video Grid --- */}
      <section>
        <div className="flex items-center gap-2 mb-4">
          <button
            className={`px-4 py-1.5 rounded-lg text-sm font-medium ${
              videoTab === 'videos' ? 'bg-dark-700 text-white' : 'text-gray-500 hover:text-white'
            }`}
            onClick={() => setVideoTab('videos')}
          >Videos</button>
          <button
            className={`px-4 py-1.5 rounded-lg text-sm font-medium ${
              videoTab === 'shorts' ? 'bg-dark-700 text-white' : 'text-gray-500 hover:text-white'
            }`}
            onClick={() => setVideoTab('shorts')}
          >Shorts</button>
          <button
            className={`px-4 py-1.5 rounded-lg text-sm font-medium ${
              videoTab === 'live' ? 'bg-dark-700 text-white' : 'text-gray-500 hover:text-white'
            }`}
            onClick={() => setVideoTab('live')}
          >En directo</button>
          <button
            className={`px-4 py-1.5 rounded-lg text-sm font-medium ${
              videoTab === 'growth' ? 'bg-dark-700 text-white' : 'text-gray-500 hover:text-white'
            }`}
            onClick={() => setVideoTab('growth')}
          ><TrendingUp size={14} className="inline mr-1" />Crecimiento</button>
          <button
            className={`px-4 py-1.5 rounded-lg text-sm font-medium ${
              videoTab === 'promotion' ? 'bg-dark-700 text-white' : 'text-gray-500 hover:text-white'
            }`}
            onClick={() => setVideoTab('promotion')}
          ><Megaphone size={14} className="inline mr-1" />Promoción</button>
          <button
            className={`px-4 py-1.5 rounded-lg text-sm font-medium ${
              videoTab === 'slots' ? 'bg-dark-700 text-white' : 'text-gray-500 hover:text-white'
            }`}
            onClick={() => setVideoTab('slots')}
          ><Clock size={14} className="inline mr-1" />Horarios</button>
          
          {/* ── Playlist filter ── */}
          {videoTab === 'videos' && channelPlaylists.length > 0 && (
            <div className="ml-auto">
              <select
                value={filterPlaylistId || ''}
                onChange={e => setFilterPlaylistId(e.target.value ? Number(e.target.value) : null)}
                className="bg-dark-700 text-gray-300 text-xs px-3 py-1.5 rounded-lg border border-surface-border focus:outline-none focus:border-neon-red/50"
              >
                <option value="">Todas las listas</option>
                {channelPlaylists.map((pl: any) => (
                  <option key={pl.id} value={pl.id}>{pl.name}</option>
                ))}
              </select>
            </div>
          )}
        </div>
        {videoTab === 'growth' ? (
          <div className="space-y-4">
            {/* YPP Progress Card */}
            {monetizationData && (
              <div className="glass p-4 rounded-xl border border-neon-gold/10">
                <div className="flex items-center gap-2 mb-3">
                  <TrendingUp size={18} className="text-neon-gold" />
                  <h3 className="text-sm font-semibold text-gray-300">Progreso YPP</h3>
                  {monetizationData.ypp_progress?.ypp_eligible && (
                    <span className="text-[11px] px-2 py-0.5 rounded bg-green-600/20 text-green-400">¡Elegible!</span>
                  )}
                </div>
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                  <div>
                    <div className="flex items-center justify-between mb-1">
                      <span className="text-xs text-gray-400">Suscriptores</span>
                      <span className="text-xs font-mono tabular-nums text-gray-300">
                        {formatShortNumber(monetizationData.subscribers || 0)} / 1,000
                      </span>
                    </div>
                    <div className="h-2.5 bg-dark-600 rounded-full mb-1">
                      <div className="h-full bg-gradient-to-r from-neon-cyan to-neon-gold rounded-full transition-all"
                        style={{ width: `${monetizationData.ypp_progress?.subs_pct || 0}%` }} />
                    </div>
                    <span className="text-[10px] text-gray-500">{monetizationData.ypp_progress?.subs_pct || 0}% completado</span>
                    {monetizationData.ypp_progress?.estimated_days_to_1k_subs != null && monetizationData.ypp_progress?.estimated_days_to_1k_subs > 0 && (
                      <span className="text-[10px] text-gray-500 ml-2">~{monetizationData.ypp_progress.estimated_days_to_1k_subs} días</span>
                    )}
                  </div>
                  <div>
                    <div className="flex items-center justify-between mb-1">
                      <span className="text-xs text-gray-400">Horas de visualización</span>
                      <span className="text-xs font-mono tabular-nums text-gray-300">
                        {formatShortNumber(monetizationData.watch_hours || 0)} / 4,000
                      </span>
                    </div>
                    <div className="h-2.5 bg-dark-600 rounded-full mb-1">
                      <div className="h-full bg-gradient-to-r from-green-500 to-neon-gold rounded-full transition-all"
                        style={{ width: `${monetizationData.ypp_progress?.hours_pct || 0}%` }} />
                    </div>
                    <span className="text-[10px] text-gray-500">{monetizationData.ypp_progress?.hours_pct || 0}% completado</span>
                    {monetizationData.ypp_progress?.estimated_days_to_4k_hours != null && monetizationData.ypp_progress?.estimated_days_to_4k_hours > 0 && (
                      <span className="text-[10px] text-gray-500 ml-2">~{monetizationData.ypp_progress.estimated_days_to_4k_hours} días</span>
                    )}
                  </div>
                </div>
              </div>
            )}

            {/* Revenue Panel */}
            {monetizationData && (
              <div className="glass p-4 rounded-xl border border-green-500/10">
                <div className="flex items-center gap-2 mb-3">
                  <DollarSign size={18} className="text-green-400" />
                  <h3 className="text-sm font-semibold text-gray-300">Revenue Estimado</h3>
                </div>
                <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 mb-3">
                  <div className="bg-dark-800/50 rounded-lg p-3 text-center">
                    <p className="text-[10px] text-gray-500 mb-0.5">CPM</p>
                    <p className="text-sm font-mono font-semibold text-green-400">
                      ${monetizationData.cpm_min || '?'}–${monetizationData.cpm_max || '?'}
                    </p>
                  </div>
                  <div className="bg-dark-800/50 rounded-lg p-3 text-center">
                    <p className="text-[10px] text-gray-500 mb-0.5">Total est.</p>
                    <p className="text-sm font-mono font-semibold text-green-400">
                      ${monetizationData.revenue_total_min?.toFixed(0) || '0'}–${monetizationData.revenue_total_max?.toFixed(0) || '0'}
                    </p>
                  </div>
                  <div className="bg-dark-800/50 rounded-lg p-3 text-center">
                    <p className="text-[10px] text-gray-500 mb-0.5">Vertical</p>
                    <p className="text-xs text-gray-300">{monetizationData.monetization_vertical || '—'}</p>
                  </div>
                  <div className="bg-dark-800/50 rounded-lg p-3 text-center">
                    <p className="text-[10px] text-gray-500 mb-0.5">Status</p>
                    <p className="text-xs font-medium text-neon-gold">{monetizationData.ypp_status || 'No monetizado'}</p>
                  </div>
                </div>
                {monetizationData.top_revenue_videos?.length > 0 && (
                  <div>
                    <p className="text-xs text-gray-500 mb-2">Top videos por revenue:</p>
                    <div className="space-y-1">
                      {monetizationData.top_revenue_videos.map((v: any, i: number) => (
                        <div key={i} className="flex items-center justify-between text-xs bg-dark-800/30 rounded px-2 py-1">
                          <span className="text-gray-300 truncate max-w-[60%]">{v.title}</span>
                          <span className="font-mono tabular-nums text-green-400">${v.revenue_min?.toFixed(2)}–${v.revenue_max?.toFixed(2)}</span>
                        </div>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            )}

            {/* Growth Chart (simple bars) */}
            {growthData?.data && growthData.data.length > 0 && (
              <div className="glass p-4 rounded-xl">
                <div className="flex items-center justify-between mb-3">
                  <div className="flex items-center gap-2">
                    <BarChart3 size={18} className="text-neon-cyan" />
                    <h3 className="text-sm font-semibold text-gray-300">Crecimiento</h3>
                  </div>
                  <div className="flex gap-1">
                    {[30, 60, 90].map(d => (
                      <button key={d}
                        onClick={() => setGrowthDays(d)}
                        className={`px-2 py-0.5 text-[10px] rounded ${growthDays === d ? 'bg-neon-red/10 text-neon-red' : 'text-gray-500 hover:text-gray-300'}`}
                      >{d}d</button>
                    ))}
                  </div>
                </div>
                <div className="h-32 flex items-end gap-0.5">
                  {growthData.data.map((point: any, i: number) => {
                    const maxSubs = Math.max(...growthData.data.map((p: any) => p.subscribers || 0), 1)
                    const height = ((point.subscribers || 0) / maxSubs) * 100
                    return (
                      <div key={i} className="flex-1 flex flex-col items-center justify-end group relative" style={{ height: '100%' }}>
                        <div className="w-full bg-neon-cyan/40 hover:bg-neon-cyan/70 rounded-t transition-colors"
                          style={{ height: `${Math.max(1, height)}%` }}
                          title={`${point.date_key}: ${formatShortNumber(point.subscribers || 0)} subs`}
                        />
                      </div>
                    )
                  })}
                </div>
                <div className="flex items-center justify-between mt-2 text-[10px] text-gray-500">
                  {growthData.data.length > 0 && (
                    <>
                      <span>{growthData.data[0]?.date_key}</span>
                      <span className="font-mono tabular-nums text-neon-cyan">{formatShortNumber(growthData.data[growthData.data.length-1]?.subscribers || 0)} subs</span>
                    </>
                  )}
                </div>
              </div>
            )}

            {/* Watch Time Chart */}
            {watchTimeData?.daily_breakdown && watchTimeData.daily_breakdown.length > 0 && (
              <WatchTimeChart
                data={watchTimeData.daily_breakdown}
                totalWatchHours={watchTimeData.total_watch_hours}
                dailyAvgHours={watchTimeData.daily_avg_hours}
                estimatedDays={watchTimeData.estimated_days_to_4000h}
                yppProgressPct={watchTimeData.ypp_progress_pct}
                remainingHours={watchTimeData.remaining_hours}
              />
            )}

            {/* Content Ranking */}
            {contentRanking.length > 0 && (
              <div className="glass p-4 rounded-xl">
                <div className="flex items-center gap-2 mb-3">
                  <Eye size={18} className="text-neon-red" />
                  <h3 className="text-sm font-semibold text-gray-300">Ranking de Contenido</h3>
                </div>
                <div className="overflow-x-auto">
                  <table className="w-full text-xs">
                    <thead>
                      <tr className="text-gray-500 border-b border-white/5">
                        <th className="text-left py-2 font-medium">Video</th>
                        <th className="text-right py-2 font-medium">Vistas</th>
                        <th className="text-right py-2 font-medium">Watch h</th>
                        <th className="text-right py-2 font-medium">Likes</th>
                        <th className="text-right py-2 font-medium hidden sm:table-cell">Revenue est.</th>
                      </tr>
                    </thead>
                    <tbody>
                      {contentRanking.slice(0, 10).map((v: any, i: number) => (
                        <tr key={i} className="border-b border-white/5 hover:bg-dark-800/30">
                          <td className="py-2 pr-4">
                            <span className="text-gray-300 line-clamp-1">{v.title}</span>
                          </td>
                          <td className="py-2 text-right font-mono tabular-nums text-neon-cyan">{formatShortNumber(v.views)}</td>
                          <td className="py-2 text-right font-mono tabular-nums text-green-400">
                            {v.estimated_minutes_watched ? `${Math.round(v.estimated_minutes_watched / 6) / 10}h` : '—'}
                          </td>
                          <td className="py-2 text-right font-mono tabular-nums text-neon-gold">{formatShortNumber(v.likes)}</td>
                          <td className="py-2 text-right font-mono tabular-nums text-green-400 hidden sm:table-cell">
                            ${v.revenue_min?.toFixed(2)}–${v.revenue_max?.toFixed(2)}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            )}

            {/* Milestones */}
            {milestonesData?.milestones && milestonesData.milestones.length > 0 && (
              <div className="glass p-4 rounded-xl border border-neon-gold/10">
                <div className="flex items-center gap-2 mb-3">
                  <Award size={18} className="text-neon-gold" />
                  <h3 className="text-sm font-semibold text-gray-300">Hitos</h3>
                </div>
                <div className="space-y-1.5 max-h-64 overflow-y-auto">
                  {milestonesData.milestones.map((m: any, i: number) => {
                    const isAchieved = m.status === 'achieved'
                    return (
                      <div key={i} className={`flex items-center justify-between text-xs py-2 px-2 rounded ${isAchieved ? 'bg-green-600/5' : 'bg-dark-800/30'}`}>
                        <div className="flex items-center gap-2">
                          <div className={`w-2 h-2 rounded-full ${isAchieved ? 'bg-green-400' : 'bg-gray-600'}`} />
                          <span className={isAchieved ? 'text-green-400 font-medium' : 'text-gray-400'}>{m.label}</span>
                        </div>
                        <div className="flex items-center gap-3">
                          <div className="w-16 h-1.5 bg-dark-600 rounded-full hidden sm:block">
                            <div className="h-full bg-neon-gold rounded-full" style={{ width: `${m.percentage || 0}%` }} />
                          </div>
                          <span className={`font-mono tabular-nums w-12 text-right ${isAchieved ? 'text-green-400' : 'text-gray-500'}`}>
                            {isAchieved ? '✅' : `${Math.round(m.percentage || 0)}%`}
                          </span>
                          {!isAchieved && m.predicted_days != null && m.predicted_days > 0 && (
                            <span className="text-[10px] text-gray-600 hidden sm:inline">~{m.predicted_days}d</span>
                          )}
                        </div>
                      </div>
                    )
                  })}
                </div>
              </div>
            )}
          </div>
        ) : videoTab === 'promotion' ? (
          <PromotionTab
            channelId={channelId}
            videos={videos}
            playlists={playlists}
            setPlaylists={setPlaylists}
            loadingPlaylists={loadingPlaylists}
            setLoadingPlaylists={setLoadingPlaylists}
            syncingPlaylists={syncingPlaylists}
            setSyncingPlaylists={setSyncingPlaylists}
            lifecycleActions={lifecycleActions}
            setLifecycleActions={setLifecycleActions}
            loadingLifecycle={loadingLifecycle}
            setLoadingLifecycle={setLoadingLifecycle}
            selectedVideoId={selectedVideoId}
            setSelectedVideoId={setSelectedVideoId}
            promoActionResult={promoActionResult}
            setPromoActionResult={setPromoActionResult}
          />
        ) : videoTab === 'slots' ? (
          <HorariosTab channelId={channelId} />
        ) : videoTab === 'shorts' ? (
          <div>
          {nativeShortResult && (
            <div className={`mb-3 p-3 rounded-lg text-sm flex items-center gap-2 ${
              nativeShortResult.ok ? 'bg-green-600/10 border border-green-600/30 text-green-400' : 'bg-red-600/10 border border-red-600/30 text-red-400'
            }`}>
              <span>{nativeShortResult.ok ? '✅' : '❌'}</span>
              <span>{nativeShortResult.message}</span>
              {nativeShortResult.url && (
                <a href={nativeShortResult.url} target="_blank" rel="noopener noreferrer"
                   className="ml-auto text-neon-red underline text-xs">Ver en YouTube →</a>
              )}
            </div>
          )}
          {loadingShorts ? (
            <div className="text-center py-16">
              <RefreshCw size={24} className="animate-spin mx-auto text-gray-500" />
            </div>
          ) : shorts.length === 0 ? (
            <div className="text-center py-16 glass rounded-xl">
              <Video size={48} className="mx-auto mb-4 opacity-20 text-gray-600" />
              <p className="text-gray-500">No hay Shorts en este canal</p>
              <p className="text-xs text-gray-600 mt-1 mb-4">
                Los Shorts se extraen automáticamente de videos largos o se generan como nativos
              </p>
              <button
                onClick={handleGenerateNativeShort}
                disabled={generatingNativeShort}
                className="px-4 py-2 bg-neon-red/10 text-neon-red border border-neon-red/20 rounded-lg text-sm font-medium hover:bg-neon-red/20 disabled:opacity-50 transition-colors flex items-center gap-2 mx-auto"
              >
                {generatingNativeShort ? (
                  <><RefreshCw size={14} className="animate-spin" /> Generando...</>
                ) : (
                  <><Plus size={14} /> Generar Short Nativo</>
                )}
              </button>
            </div>
          ) : (
            <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 gap-3">
              {shorts.map((s: any) => (
                <div key={s.id} className="glass rounded-lg overflow-hidden group">
                    <div className="relative aspect-[9/16] bg-dark-800 flex items-center justify-center rounded-lg overflow-hidden">
                      {s.youtube_id ? (
                        (shortStats[s.youtube_id]?.embeddable === false) ? (
                          <div className="w-full h-full flex flex-col items-center justify-center bg-dark-800 cursor-pointer p-2"
                               onClick={() => window.open(s.youtube_url || `https://www.youtube.com/shorts/${s.youtube_id}`, '_blank', 'noopener')}>
                            <div className="w-8 h-8 rounded-full bg-neon-red/80 flex items-center justify-center mb-1">
                              <ExternalLink size={14} className="text-white" />
                            </div>
                            <p className="text-[9px] text-gray-400 text-center leading-tight">
                              Embed bloqueado<br />Ver en YouTube
                            </p>
                          </div>
                        ) : (
                          <iframe
                            src={`https://www.youtube.com/embed/${s.youtube_id}`}
                            title="YouTube Shorts player"
                            allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture"
                            allowFullScreen
                            className="w-full h-full"
                          />
                        )
                      ) : s.file_path ? (
                        <video src={apiUrl(`/static/${s.file_path}?v=${s.id}`)} className="w-full h-full object-cover" preload="metadata" controls />
                      ) : (
                        <Video size={24} className="text-gray-600" />
                      )}
                    <span className={`absolute top-1.5 right-1.5 text-[10px] px-1.5 py-0.5 rounded font-medium ${
                      s.status === 'published' ? 'bg-green-600/90 text-white' :
                      s.status === 'ready' ? 'bg-blue-600/90 text-white' :
                      s.status === 'failed' ? 'bg-red-600/90 text-white' :
                      'bg-gray-600/90 text-white'
                    }`}>
                      {s.status === 'published' ? 'Publicado' : s.status === 'ready' ? 'Listo' : s.status === 'failed' ? 'Error' : 'Pendiente'}
                    </span>
                  </div>
                  <div className="p-2">
                    <p className="text-xs text-gray-300 truncate">{s.hook_title || s.title || 'Short'}</p>
                    {s.duration && (
                      <p className="text-[10px] text-gray-500 mt-0.5">{formatDuration(s.duration)}</p>
                    )}
                    <p className="text-[10px] text-gray-600 mt-0.5">{formatDateTime(s.published_at || s.created_at)}</p>
                    {s.scheduled_date && (
                      <p className="text-[10px] text-gray-500 mt-0.5">{formatDate(s.scheduled_date)}</p>
                    )}
                    {s.youtube_id && shortStats[s.youtube_id] && (
                      <div className="flex items-center gap-2 mt-0.5 text-[10px] text-gray-500">
                        <span>{formatShortNumber(shortStats[s.youtube_id].viewCount || '0')} vistas</span>
                        <span>·</span>
                        <span>{formatShortNumber(shortStats[s.youtube_id].likeCount || '0')} likes</span>
                      </div>
                    )}
                    {s.youtube_url && (
                      <a href={s.youtube_url} target="_blank" rel="noopener noreferrer"
                         className="text-[10px] text-neon-red hover:underline mt-0.5 block">
                        Ver en YouTube &rarr;
                      </a>
                    )}
                  </div>
                </div>
              ))}
            </div>
          )}
          </div>
        ) : (
          <div>
          {videos.length === 0 ? (
          <div className="text-center py-16 glass rounded-xl">
            <Video size={48} className="mx-auto mb-4 opacity-20 text-gray-600" />
            <p className="text-gray-500">No hay videos en este canal</p>
            <p className="text-xs text-gray-600 mt-1">Genera tu primer video usando el panel de arriba</p>
          </div>
        ) : (
          <div className="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 gap-3 sm:gap-4">
            {videos.map((v: any) => {
              // If YouTube ID exists, the video was successfully uploaded regardless
              // of what status says (orphan-detector / zombie-thread races can
              // overwrite it to 'error').
              const displayStatus = v.yt_video_id ? v.status === 'uploaded_private' || v.status === 'warming' || v.status === 'scheduled' || v.status === 'published' ? v.status : 'uploaded' : (v.status || 'draft');
              const hasScheduledInfo = v.target_public_at && ['uploaded_private', 'warming', 'scheduled'].includes(v.status);
              const pendingManual = (v.manual_altered_content_done ? 0 : 1) + (v.manual_end_screens_done ? 0 : 1);
              return (
              <div key={v.id} className={`group cursor-pointer ${(v.source_mode === 'viral' || v.source_url) ? 'border-l-[3px] border-amber-500/70 shadow-[inset_4px_0_12px_-4px_rgba(245,158,11,0.15)]' : 'border-l-[3px] border-transparent'}`} onClick={() => navigate(`/videos/${v.id}/edit`)}>
                <div className="relative aspect-video rounded-xl overflow-hidden bg-dark-700 mb-2">
                  {v.thumbnail_path ? (
                    <img src={apiUrl(`/thumbnail/${v.id}?v=${v.updated_at || v.id}`)} alt={v.titulo_final} className="w-full h-full object-cover group-hover:scale-110 transition-transform duration-500" />
                  ) : (
                    <div className="w-full h-full flex items-center justify-center bg-gradient-to-br from-dark-700 to-dark-900"><Video size={28} className="text-gray-700" /></div>
                  )}
                  {v.duracion_seg && <span className="absolute bottom-1.5 right-1.5 bg-black/85 text-white text-[11px] px-1.5 py-0.5 rounded font-mono">{formatDuration(v.duracion_seg)}</span>}
                  <span className={`absolute top-1.5 left-1.5 text-[10px] px-1.5 py-0.5 rounded font-medium badge ${statusBadge(displayStatus)}`}>
                    {statusLabel(displayStatus)}
                  </span>
                  {(v.source_mode === 'viral' || v.source_url) && (
                    <span className="absolute top-1.5 right-1.5 text-[10px] px-1.5 py-0.5 rounded font-bold flex items-center gap-1 bg-amber-500/20 text-amber-400 border border-amber-500/30 shadow-sm">
                      <Zap size={10} className="text-amber-400" /> VIRAL
                    </span>
                  )}
                  <div className="absolute inset-0 flex items-center justify-center opacity-0 group-hover:opacity-100 transition-opacity duration-300 bg-black/20">
                    <div className="w-12 h-12 rounded-full bg-neon-red/90 flex items-center justify-center shadow-lg"><Play size={20} className="text-white ml-0.5" /></div>
                  </div>
                </div>
                <div>
                  <p className="text-sm font-medium text-white leading-tight line-clamp-2 group-hover:text-neon-red transition-colors">{v.titulo_final || 'Video sin título'}</p>
                  <div className="flex items-center gap-1.5 mt-0.5 text-xs text-gray-600">
                    <span>{formatDateTime(v.uploaded_at || v.created_at)}</span>
                    {v.target_playlist_name && (
                      <span className="text-[11px] bg-neon-purple/10 text-neon-purple/80 px-1.5 py-0.5 rounded-full flex items-center gap-1">
                        <ListPlus size={10} /> {v.target_playlist_name}
                      </span>
                    )}
                    {v.yt_url && <><span>·</span><a href={v.yt_url} target="_blank" rel="noopener noreferrer" className="text-neon-red hover:underline flex items-center gap-0.5" onClick={e => e.stopPropagation()}><Youtube size={10} /> YT</a></>}
                    {v.source_url && <><span>·</span><a href={v.source_url} target="_blank" rel="noopener noreferrer" className="text-purple-400 hover:underline flex items-center gap-0.5 text-xs" onClick={e => e.stopPropagation()} title="Video original (viral mirror)"><ExternalLink size={10} /> Original</a></>}
                    {v.script_id && (
                      creatingShort === v.id ? (
                        <button disabled className="text-purple-400 text-xs flex items-center gap-1 px-2 py-0.5 rounded bg-purple-400/10 cursor-wait">
                          <Loader2 size={12} className="animate-spin" /> Extrayendo clip...
                        </button>
                      ) : (
                        <button onClick={e => { e.preventDefault(); e.stopPropagation(); handleCreateShort(v.id) }} 
                          className="text-purple-400 hover:text-purple-300 text-xs flex items-center gap-1 px-2 py-0.5 rounded hover:bg-purple-400/10 transition-colors"
                          title="Crear Short de este video">
                          <Scissors size={12} /> Short
                        </button>
                      )
                    )}
                  </div>
                  
                  {/* ── Scheduled publish info strip ── */}
                  {hasScheduledInfo && (
                    <div className="mt-2 pt-2 border-t border-surface-border/50 space-y-1">
                      <div className="flex items-center gap-1.5">
                        <Clock size={10} className="text-neon-gold" />
                        <span className="text-[10px] text-neon-gold font-mono tabular-nums">
                          {formatCountdown(v.target_public_at)}
                        </span>
                      </div>
                      {v.target_playlist_name && (
                        <div className="flex items-center gap-1">
                          <ListPlus size={10} className="text-neon-purple" />
                          <span className="text-[10px] text-neon-purple/70">{v.target_playlist_name}</span>
                        </div>
                      )}
                      {pendingManual > 0 && (
                        <span className="inline-flex items-center gap-1 text-[10px] text-neon-red bg-neon-red/10 px-1.5 py-0.5 rounded-full">
                          <AlertTriangle size={10} /> {pendingManual}
                        </span>
                      )}
                    </div>
                  )}

                  {v.yt_video_id && videoStats[v.yt_video_id] && videoStats[v.yt_video_id].viewCount ? (
                    <div className="flex items-center gap-2 mt-0.5 text-[11px] text-gray-500">
                      <span>{formatShortNumber(videoStats[v.yt_video_id].viewCount || '0')} vistas</span>
                      <span>·</span>
                      <span>{formatShortNumber(videoStats[v.yt_video_id].likeCount || '0')} likes</span>
                      <VideoTiming timing={v.timing_data} className="!mt-0" />
                    </div>
                  ) : (
                    <VideoTiming timing={v.timing_data} />
                  )}
                  <div className="flex gap-1 mt-1.5 opacity-0 group-hover:opacity-100 transition-opacity">
                    {v.thumbnail_path && <button onClick={e => { e.preventDefault(); e.stopPropagation(); handleDownloadThumbnail(v.id) }} className="text-[11px] text-neon-cyan bg-neon-cyan/10 px-2 py-0.5 rounded hover:bg-neon-cyan/20 flex items-center gap-1"><Download size={11} /> Mini</button>}
                    {v.status === 'ready' && !v.yt_video_id && <button onClick={e => { e.preventDefault(); handleUpload(v.id) }} className="text-[11px] text-neon-red bg-neon-red/10 px-2 py-0.5 rounded hover:bg-neon-red/20">Subir a YT</button>}
                    {v.yt_video_id && <button onClick={e => { e.preventDefault(); handleUpload(v.id) }} className="text-[11px] text-neon-gold bg-neon-gold/10 px-2 py-0.5 rounded hover:bg-neon-gold/20">Resubir</button>}
                    {(displayStatus === 'error' || displayStatus === 'failed') && <button onClick={e => { e.preventDefault(); setDeleteTarget(v.id) }} className="text-[11px] text-red-400 bg-red-400/10 px-2 py-0.5 rounded hover:bg-red-400/20 flex items-center gap-1"><Trash2 size={11} /> Eliminar</button>}
                  </div>
                </div>
              </div>
              );
            })}
          </div>
        )}
          </div>
        )}
      </section>

      {/* Short result notification — stays until dismissed */}
      {shortResult && (
        <div className={`mt-4 p-3 rounded-lg text-sm flex items-center gap-2 animate-slide-up ${
          shortResult.ok ? 'bg-green-600/10 border border-green-600/30 text-green-400' : 'bg-red-600/10 border border-red-600/30 text-red-400'
        }`}>
          <span>{shortResult.ok ? '✅' : '❌'}</span>
          <span className="flex-1">{shortResult.message}</span>
          {shortResult.url && (
            <a href={shortResult.url} target="_blank" rel="noopener noreferrer" 
               className="text-neon-red underline text-xs whitespace-nowrap">Ver en YouTube →</a>
          )}
          <button onClick={() => setShortResult(null)} className="ml-1 p-1 rounded hover:bg-white/10 transition-colors" title="Cerrar">
            <X size={14} />
          </button>
        </div>
      )}

      {/* --- Edit Profile Modal --- */}
      {editingProfile && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm" onClick={() => setEditingProfile(false)}>
          <div className="glass rounded-xl p-5 sm:p-6 w-full max-w-lg mx-4 sm:mx-0 space-y-4 animate-slide-up max-h-[90vh] overflow-y-auto" onClick={e => e.stopPropagation()}>
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
              <div><label className="block text-xs text-gray-400 mb-1">Cuenta de Google</label>
                <input type="text" value={profileForm.google_account || ''} onChange={e => setProfileForm({ ...profileForm, google_account: e.target.value })}
                  className="w-full px-3 py-2 bg-dark-700 border border-surface-border rounded-lg text-white text-sm focus:outline-none focus:border-neon-red" placeholder="email@gmail.com" /></div>
              <div><label className="block text-xs text-gray-400 mb-1">URL de YouTube Studio</label>
                <input type="text" value={profileForm.yt_studio_url || ''} onChange={e => setProfileForm({ ...profileForm, yt_studio_url: e.target.value })}
                  className="w-full px-3 py-2 bg-dark-700 border border-surface-border rounded-lg text-white text-sm focus:outline-none focus:border-neon-red" placeholder="https://studio.youtube.com/channel/..." /></div>
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

      {/* --- Source Mode Modal --- */}
      {showSourceModeModal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm" onClick={() => setShowSourceModeModal(false)}>
          <div className="glass rounded-xl p-6 w-full max-w-md mx-4 animate-slide-up space-y-5" onClick={e => e.stopPropagation()}>
            <div className="text-center">
              <div className="w-14 h-14 mx-auto mb-3 rounded-full bg-neon-cyan/10 flex items-center justify-center">
                <Wand2 size={28} className="text-neon-cyan" />
              </div>
              <h3 className="font-display text-xl font-semibold text-white">¿Que metodo usar?</h3>
              <p className="text-sm text-gray-400 mt-2">
                Elige como se creara el contenido del video.
              </p>
            </div>
            <div className="flex gap-3">
              <button
                onClick={() => { setSourceMode('original'); setShowUploadConfirm(true) }}
                className="flex-1 py-4 px-4 bg-dark-700 border border-surface-border text-gray-300 rounded-xl font-medium text-sm hover:bg-dark-600 hover:border-gray-600 transition-all flex flex-col items-center gap-2">
                <RefreshCw size={20} className="text-gray-400" />
                <span className="font-semibold">Original</span>
                <span className="text-[10px] text-gray-500 text-center">Generar contenido nuevo con IA desde fuentes habituales</span>
              </button>
              <button
                onClick={() => { setSourceMode('viral'); setShowUploadConfirm(true) }}
                className="flex-1 py-4 px-4 bg-gradient-to-r from-neon-purple/20 to-purple-600/20 border border-purple-500/30 text-purple-300 rounded-xl font-medium text-sm hover:border-purple-400 hover:bg-purple-600/10 transition-all flex flex-col items-center gap-2">
                <TrendingUp size={20} className="text-purple-400" />
                <span className="font-semibold text-white">Viral</span>
                <span className="text-[10px] text-gray-400 text-center">Buscar video viral en ingles, traducir y adaptar</span>
              </button>
            </div>
          </div>
        </div>
      )}

      {/* --- Upload Confirmation Modal --- */}
      {showUploadConfirm && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm" onClick={() => setShowUploadConfirm(false)}>
          <div className="glass rounded-xl p-6 w-full max-w-md mx-4 animate-slide-up space-y-5" onClick={e => e.stopPropagation()}>
            <div className="text-center">
              <div className="w-14 h-14 mx-auto mb-3 rounded-full bg-neon-red/10 flex items-center justify-center">
                <Upload size={28} className="text-neon-red" />
              </div>
              <h3 className="font-display text-xl font-semibold text-white">¿Subir directamente a YouTube?</h3>
              <p className="text-sm text-gray-400 mt-2">
                El video se generará con calidad completa. Puedes subirlo ahora o dejarlo en estado "Listo" y subirlo más tarde manualmente.
              </p>
            </div>
            <div className="flex gap-3">
              <button
                onClick={() => handleGenerate(false, sourceMode)}
                className="flex-1 py-3 px-4 bg-dark-700 border border-surface-border text-gray-300 rounded-xl font-medium text-sm hover:bg-dark-600 hover:border-gray-600 transition-all flex items-center justify-center gap-2">
                <Eye size={16} />
                No, solo generar
              </button>
              <button
                onClick={() => handleGenerate(true, sourceMode)}
                className="flex-1 py-3 px-4 bg-gradient-to-r from-neon-red to-red-600 text-white rounded-xl font-bold text-sm hover:shadow-lg hover:shadow-neon-red/20 transition-all flex items-center justify-center gap-2">
                <Upload size={16} />
                Sí, subir
              </button>
            </div>
            <p className="text-[10px] text-gray-600 text-center">
              {sourceMode === 'viral' 
                ? 'Modo Viral: se buscara un video en YouTube en ingles, se traducira y adaptara.' 
                : '"Sí, subir" — genera y publica automaticamente. "No, solo generar" — el video quedara en "Listo" con boton para subir despues.'}
            </p>
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

          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
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
          <div className="glass rounded-xl p-5 sm:p-6 w-full max-w-lg mx-4 sm:mx-0 space-y-4 animate-slide-up" onClick={e => e.stopPropagation()}>
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
          <div className="glass rounded-xl p-5 sm:p-6 w-full max-w-lg mx-4 sm:mx-0 space-y-4 animate-slide-up max-h-[85vh] overflow-y-auto" onClick={e => e.stopPropagation()}>
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

      {/* --- Template Result Modal --- */}
      {templateResult && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm" onClick={() => setTemplateResult(null)}>
          <div className="glass rounded-xl p-5 sm:p-6 w-full max-w-sm mx-4 sm:mx-0 space-y-4 animate-slide-up text-center" onClick={e => e.stopPropagation()}>
            <div className={`text-4xl ${templateResult.ok ? 'text-green-400' : 'text-red-400'}`}>
              {templateResult.ok ? '✅' : '❌'}
            </div>
            <p className="text-sm text-gray-300 leading-relaxed">{templateResult.message}</p>
            <button
              onClick={() => setTemplateResult(null)}
              className="w-full py-2 bg-dark-600 text-gray-300 rounded-lg text-sm hover:bg-dark-500 transition"
            >
              Cerrar
            </button>
          </div>
        </div>
      )}

      {/* --- Delete Confirm Modal --- */}
      {deleteTarget !== null && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm" onClick={() => setDeleteTarget(null)}>
          <div className="glass rounded-xl p-5 sm:p-6 w-full max-w-sm mx-4 sm:mx-0 space-y-4 animate-slide-up" onClick={e => e.stopPropagation()}>
            <div className="flex items-center gap-2 text-red-400">
              <Trash2 size={20} />
              <h3 className="font-display text-lg font-semibold text-white">Eliminar video</h3>
            </div>
            <p className="text-sm text-gray-400">
              ¿Seguro que quieres eliminar este video? Esta acción no se puede deshacer.
            </p>
            <div className="flex gap-2 justify-end">
              <button onClick={() => setDeleteTarget(null)}
                className="px-4 py-2 bg-dark-600 text-gray-300 rounded-lg text-sm hover:bg-dark-500 transition">
                Cancelar
              </button>
              <button onClick={() => handleDelete(deleteTarget!)}
                className="px-4 py-2 bg-red-600 text-white rounded-lg text-sm font-medium hover:bg-red-500 transition flex items-center gap-1.5">
                <Trash2 size={14} /> Eliminar
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
