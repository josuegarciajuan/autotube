import { useState, useEffect } from 'react'
import { useParams, Link } from 'react-router-dom'
import { api, formatDuration, formatDate, formatDateTime, formatTimingMs, statusBadge, statusLabel, truncate, mediaUrl, apiUrl } from '../lib/api'
import { ArrowLeft, Play, Pause, Save, Upload, Image, Volume2, RefreshCw, Film, Edit3, Wand2, CheckCircle, XCircle, Loader2, ExternalLink } from 'lucide-react'
import { useGenerationProgress } from '../hooks/useWebSocket'
import ScheduledPublishPanel from '../components/ScheduledPublishPanel'
import ManualChecklistCard from '../components/ManualChecklistCard'

export default function VideoEditor() {
  const { id } = useParams<{ id: string }>()
  const videoId = Number(id)

  const [video, setVideo] = useState<any>(null)
  const [scenes, setScenes] = useState<any[]>([])
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [editingScene, setEditingScene] = useState<number | null>(null)
  const [editText, setEditText] = useState('')

  // Metadata editing
  const [title, setTitle] = useState('')
  const [description, setDescription] = useState('')
  const [showMetadataEditor, setShowMetadataEditor] = useState(false)

  // Marketing AI
  const [marketing, setMarketing] = useState<any>(null)
  const [loadingMarketing, setLoadingMarketing] = useState(false)
  const [showMarketing, setShowMarketing] = useState(false)

  const [uploadJobId, setUploadJobId] = useState<number | null>(null)
  const [uploadToast, setUploadToast] = useState<{ type: 'success' | 'error'; message: string } | null>(null)
  const { progress } = useGenerationProgress(uploadJobId)

  useEffect(() => {
    loadVideo()
  }, [videoId])

  async function loadVideo() {
    setLoading(true)
    try {
      const v = await api.getVideo(videoId)
      setVideo(v)
      setScenes(v.scenes || [])
      setTitle(v.titulo_final || '')
      setDescription(v.description || '')
    } catch (e) {
      console.error(e)
    }
    setLoading(false)
  }

  async function handleSaveMetadata() {
    setSaving(true)
    try {
      await api.updateVideo(videoId, { titulo_final: title, description })
    } catch (e: any) {
      alert('Error: ' + e.message)
    }
    setSaving(false)
    setShowMetadataEditor(false)
  }

  async function handleSaveScene(sceneId: number) {
    setSaving(true)
    try {
      await api.updateScene(sceneId, { script_text: editText })
      setEditingScene(null)
      loadVideo()
    } catch (e: any) {
      alert('Error: ' + e.message)
    }
    setSaving(false)
  }

  async function handleRegenerateAudio(sceneId: number) {
    try {
      await api.regenerateSceneAudio(sceneId)
      alert('Audio regenerándose... Recarga en unos segundos.')
    } catch (e: any) {
      alert('Error: ' + e.message)
    }
  }

  async function handleReplaceImage(sceneId: number) {
    try {
      await api.replaceSceneImage(sceneId)
      alert('Imagen reemplazándose... Recarga en unos segundos.')
    } catch (e: any) {
      alert('Error: ' + e.message)
    }
  }

  async function handleRegenerateThumbnail() {
    try {
      await api.regenerateThumbnail(videoId)
      alert('Miniatura regenerándose... Recarga en unos segundos.')
    } catch (e: any) {
      alert('Error: ' + e.message)
    }
  }

  async function handleGenerateMarketing() {
    setLoadingMarketing(true)
    setShowMarketing(true)
    try {
      const data = await api.generateMarketingMetadata(videoId)
      setMarketing(data)
    } catch (e: any) {
      alert('Error: ' + e.message)
      setShowMarketing(false)
    }
    setLoadingMarketing(false)
  }

  function applyMarketingTitle(t: string) {
    setTitle(t)
  }

  async function handleUpload() {
    try {
      const res = await api.uploadVideo(videoId)
      setUploadJobId(res.job_id)
      setUploadToast(null) // clear any previous toast
    } catch (e: any) {
      setUploadToast({ type: 'error', message: 'Error al iniciar subida: ' + e.message })
    }
  }

  // React to upload progress changes
  useEffect(() => {
    if (!progress) return
    if (progress.status === 'completed') {
      setUploadToast({ type: 'success', message: progress.message || 'Subido con exito' })
      setUploadJobId(null)
      loadVideo()
    } else if (progress.status === 'failed') {
      setUploadToast({ type: 'error', message: progress.message || 'Error en la subida' })
      setUploadJobId(null)
      loadVideo()
    }
  }, [progress?.status])

  function openSceneEditor(scene: any) {
    setEditingScene(scene.id)
    setEditText(scene.script_text || scene.description || '')
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="animate-spin rounded-full h-8 w-8 border-2 border-neon-red border-t-transparent" />
      </div>
    )
  }

  if (!video) {
    return (
      <div className="text-center py-16 text-gray-500">
        <Film size={48} className="mx-auto mb-4 opacity-30" />
        Video no encontrado
      </div>
    )
  }

  return (
    <div className="max-w-7xl mx-auto space-y-6 animate-fade-in">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
        <div>
          <Link to={`/channels/${video.channel_id}`} className="text-xs sm:text-sm text-gray-500 hover:text-white flex items-center gap-1 mb-1">
            <ArrowLeft size={14} /> Volver al canal
          </Link>
          <h2 className="font-display text-lg sm:text-xl font-bold text-white flex items-center gap-2">
            <Edit3 size={18} className="text-neon-red" />
            {video.titulo_final || 'Video sin título'}
          </h2>
          <div className="flex items-center gap-2 mt-1">
            <span className={`badge ${statusBadge(video.status || 'draft')}`}>{statusLabel(video.status || 'draft')}</span>
            {video.duracion_seg && <span className="text-xs text-gray-500">{formatDuration(video.duracion_seg)}</span>}
            <span className="text-xs text-gray-500">{formatDateTime(video.uploaded_at || video.created_at)}{video.timing_data?.total_duration_ms ? ` — ${formatTimingMs(video.timing_data.total_duration_ms)}` : ''}</span>
          </div>
        </div>

        <div className="flex flex-wrap gap-1.5 sm:gap-2">
          <button
            onClick={() => setShowMetadataEditor(!showMetadataEditor)}
            className="flex items-center gap-1.5 px-2.5 sm:px-3 py-2 bg-dark-700 border border-surface-border rounded-lg text-xs sm:text-sm text-gray-300 hover:bg-dark-600 transition-colors"
          >
            <Save size={14} />
            <span className="hidden sm:inline">Metadata</span>
          </button>
          <button
            onClick={handleGenerateMarketing}
            disabled={loadingMarketing}
            className="flex items-center gap-1.5 px-2.5 sm:px-3 py-2 bg-neon-gold/10 border border-neon-gold/30 rounded-lg text-xs sm:text-sm text-neon-gold hover:bg-neon-gold/20 transition-colors disabled:opacity-50"
          >
            <Wand2 size={14} />
            <span className="hidden sm:inline">{loadingMarketing ? 'Generando...' : 'Marketing IA'}</span>
          </button>
          <button
            onClick={handleRegenerateThumbnail}
            className="flex items-center gap-1.5 px-2.5 sm:px-3 py-2 bg-dark-700 border border-surface-border rounded-lg text-xs sm:text-sm text-gray-300 hover:bg-dark-600 transition-colors"
          >
            <Image size={14} />
            <span className="hidden sm:inline">Miniatura</span>
          </button>
          <button
            onClick={handleUpload}
            disabled={uploadJobId !== null}
            className="flex items-center gap-1.5 px-3 sm:px-4 py-2 bg-neon-red text-white rounded-lg text-xs sm:text-sm font-medium hover:bg-neon-red/80 transition-colors disabled:opacity-60 disabled:cursor-not-allowed"
          >
            {uploadJobId !== null ? (
              <Loader2 size={14} className="animate-spin" />
            ) : (
              <Upload size={14} />
            )}
            {uploadJobId !== null ? 'Subiendo...' : video.yt_video_id ? 'Resubir' : 'Subir a YT'}
          </button>
        </div>
      </div>

      {/* Upload progress bar */}
      {uploadJobId !== null && progress && (
        <div className="glass rounded-xl p-4 space-y-2 animate-slide-up border border-neon-red/30">
          <div className="flex items-center justify-between text-sm">
            <span className="text-white font-medium flex items-center gap-2">
              <Loader2 size={14} className="animate-spin text-neon-red" />
              {progress.message || 'Subiendo video a YouTube...'}
            </span>
            <span className="text-gray-400">{progress.progress}%</span>
          </div>
          <div className="w-full h-2 bg-dark-700 rounded-full overflow-hidden">
            <div
              className="h-full bg-neon-red rounded-full transition-all duration-500"
              style={{ width: `${progress.progress || 0}%` }}
            />
          </div>
          {progress.detail && (
            <p className="text-xs text-gray-500">{progress.detail}</p>
          )}
        </div>
      )}

      {/* Upload toast — success or error */}
      {uploadToast && (
        <div
          className={`glass rounded-xl p-4 flex items-center gap-3 animate-slide-up border ${
            uploadToast.type === 'success'
              ? 'border-green-500/40 bg-green-500/5'
              : 'border-red-500/40 bg-red-500/5'
          }`}
        >
          {uploadToast.type === 'success' ? (
            <CheckCircle size={20} className="text-green-400 shrink-0" />
          ) : (
            <XCircle size={20} className="text-red-400 shrink-0" />
          )}
          <div className="flex-1 min-w-0">
            <p className={`text-sm font-medium ${uploadToast.type === 'success' ? 'text-green-300' : 'text-red-300'}`}>
              {uploadToast.type === 'success' ? 'Subida completada' : 'Error en la subida'}
            </p>
            <p className="text-xs text-gray-400 mt-0.5 truncate">{uploadToast.message}</p>
          </div>
          <button
            onClick={() => setUploadToast(null)}
            className="text-gray-500 hover:text-white transition-colors shrink-0"
          >
            x
          </button>
        </div>
      )}

      {/* Metadata Editor */}
      {showMetadataEditor && (
        <div className="glass rounded-xl p-5 space-y-3 animate-slide-up">
          <h3 className="font-display text-sm font-semibold text-white flex items-center gap-2">
            <Wand2 size={16} className="text-neon-gold" />
            Editar Metadata
          </h3>
          <div>
            <label className="block text-xs text-gray-400 mb-1">Título</label>
            <input
              type="text"
              value={title}
              onChange={e => setTitle(e.target.value)}
              className="w-full px-3 py-2 bg-dark-700 border border-surface-border rounded-lg text-white text-sm focus:outline-none focus:border-neon-red"
            />
          </div>
          <div>
            <label className="block text-xs text-gray-400 mb-1">Descripción (YouTube)</label>
            <textarea
              value={description}
              onChange={e => setDescription(e.target.value)}
              rows={5}
              className="w-full px-3 py-2 bg-dark-700 border border-surface-border rounded-lg text-white text-sm focus:outline-none focus:border-neon-red resize-none"
            />
          </div>
          <button
            onClick={handleSaveMetadata}
            disabled={saving}
            className="px-4 py-2 bg-neon-red text-white rounded-lg text-sm font-medium hover:bg-neon-red/80 disabled:opacity-50"
          >
            {saving ? 'Guardando...' : 'Guardar Metadata'}
          </button>
        </div>
      )}

      {/* Marketing AI Panel */}
      {showMarketing && (
        <div className="glass rounded-xl p-5 space-y-4 animate-slide-up border border-neon-gold/30">
          <h3 className="font-display text-sm font-semibold text-neon-gold flex items-center gap-2">
            <Wand2 size={16} />
            {loadingMarketing ? 'Generando metadata viral...' : 'Metadata Viral Generada por IA'}
          </h3>
          
          {loadingMarketing ? (
            <div className="flex items-center gap-3 text-gray-400 text-sm">
              <div className="animate-spin rounded-full h-4 w-4 border-2 border-neon-gold border-t-transparent" />
              Consultando al especialista en marketing...
            </div>
          ) : marketing ? (
            <div className="space-y-4">
              {/* Titles */}
              <div>
                <p className="text-xs text-gray-500 font-medium mb-2">🎯 Títulos virales (click para usar)</p>
                <div className="flex flex-wrap gap-2">
                  {marketing.titles?.map((t: string, i: number) => (
                    <button
                      key={i}
                      onClick={() => { applyMarketingTitle(t); setTitle(t) }}
                      className="px-3 py-1.5 bg-neon-gold/10 border border-neon-gold/20 rounded-lg text-sm text-neon-gold hover:bg-neon-gold/20 hover:border-neon-gold/40 transition-all text-left"
                    >
                      {t}
                    </button>
                  ))}
                </div>
              </div>

              {/* Thumbnail text */}
              {marketing.thumbnail_text && (
                <div>
                  <p className="text-xs text-gray-500 font-medium mb-1">🖼️ Texto para miniatura</p>
                  <p className="text-lg font-display font-bold text-neon-red bg-dark-700/50 rounded-lg p-3 inline-block">
                    {marketing.thumbnail_text}
                  </p>
                </div>
              )}

              {/* Description */}
              <div>
                <p className="text-xs text-gray-500 font-medium mb-1">📝 Descripción</p>
                <pre className="text-sm text-gray-300 bg-dark-800 rounded-lg p-3 whitespace-pre-wrap max-h-48 overflow-y-auto font-body">
                  {marketing.description}
                </pre>
                <button
                  onClick={() => { setDescription(marketing.description); setShowMetadataEditor(true) }}
                  className="mt-2 px-3 py-1 bg-neon-gold/10 border border-neon-gold/20 text-neon-gold rounded text-xs hover:bg-neon-gold/20 transition-colors"
                >
                  Usar esta descripción
                </button>
              </div>

              {/* Tags */}
              {marketing.tags && marketing.tags.length > 0 && (
                <div>
                  <p className="text-xs text-gray-500 font-medium mb-1">🏷️ Tags SEO</p>
                  <div className="flex flex-wrap gap-1.5">
                    {marketing.tags.map((tag: string, i: number) => (
                      <span key={i} className="px-2 py-0.5 bg-dark-700 rounded-full text-xs text-gray-400 border border-surface-border">
                        #{tag}
                      </span>
                    ))}
                  </div>
                </div>
              )}
            </div>
          ) : null}
        </div>
      )}

      {/* Video Player — YouTube embed if uploaded, local file otherwise */}
      <div className="glass rounded-xl overflow-hidden">
        {video.yt_video_id ? (
          video.embeddable !== false ? (
            <div className="aspect-video">
              <iframe
                src={`https://www.youtube.com/embed/${video.yt_video_id}`}
                title="YouTube video player"
                allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture"
                allowFullScreen
                className="w-full h-full rounded-lg"
              />
            </div>
          ) : (
            <div className="aspect-video relative bg-dark-800 flex items-center justify-center group cursor-pointer"
                 onClick={() => window.open(video.yt_url || `https://www.youtube.com/watch?v=${video.yt_video_id}`, '_blank', 'noopener')}>
              {video.thumbnail_path ? (
                <img src={apiUrl(`/thumbnail/${videoId}?v=${video.updated_at || videoId}`)}
                     alt={video.titulo_final} className="w-full h-full object-cover opacity-60" />
              ) : null}
              <div className="absolute inset-0 flex flex-col items-center justify-center gap-3 bg-dark-900/70">
                <div className="w-14 h-14 rounded-full bg-neon-red flex items-center justify-center shadow-lg">
                  <ExternalLink size={24} className="text-white" />
                </div>
                <p className="text-white text-sm font-medium text-center px-4 leading-relaxed">
                  YouTube bloqueó el embed de este video<br />
                  <span className="text-gray-400 text-xs">(Copyright, Content ID o políticas de YouTube)</span>
                </p>
                <span className="text-neon-red text-xs font-bold flex items-center gap-1 hover:underline bg-neon-red/10 px-3 py-1.5 rounded-full">
                  <ExternalLink size={12} /> Ver en YouTube
                </span>
              </div>
            </div>
          )
        ) : video.video_path ? (
          <video
            src={apiUrl(`/video-file/${videoId}`)}
            controls
            className="w-full rounded-lg"
            poster={apiUrl(`/thumbnail/${videoId}?v=${video.updated_at || videoId}`)}
            preload="metadata"
          >
            Tu navegador no soporta video HTML5.
          </video>
        ) : (
          <div className="aspect-video flex items-center justify-center bg-dark-800">
            <p className="text-gray-500 text-sm">Video no disponible localmente</p>
          </div>
        )}
      </div>

      {/* ── Scheduled Publishing Panel ── */}
      {(video.status === 'uploaded_private' || video.status === 'warming' ||
        video.status === 'scheduled' || video.status === 'published' ||
        video.target_public_at) && (
        <ScheduledPublishPanel videoId={videoId} onRefresh={loadVideo} />
      )}

      {/* ── Manual Checklist ── */}
      {video.yt_video_id && (
        <ManualChecklistCard
          videoId={videoId}
          channelId={video.channel_id}
          ytVideoId={video.yt_video_id}
          alteredContentDone={video.manual_altered_content_done}
          endScreensDone={video.manual_end_screens_done}
          onRefresh={loadVideo}
        />
      )}

      {/* Scenes Editor */}
      <div className="glass rounded-xl p-5">
        <h3 className="font-display text-lg font-semibold text-white mb-4 flex items-center gap-2">
          <Film size={18} className="text-neon-red" />
          Escenas ({scenes.length})
        </h3>

        {scenes.length === 0 ? (
          <div className="text-center py-8 text-gray-500">
            <p>No hay escenas registradas para este video</p>
          </div>
        ) : (
          <div className="space-y-3">
            {scenes.map((scene: any, idx: number) => (
              <div
                key={scene.id}
                className={`bg-dark-700/50 rounded-xl border transition-all ${
                  editingScene === scene.id
                    ? 'border-neon-red/60'
                    : 'border-surface-border hover:border-neon-red/30'
                }`}
              >
                {/* Scene header */}
                <div className="flex items-center justify-between p-3">
                  <div className="flex items-center gap-3">
                    <span className="w-6 h-6 rounded-full bg-neon-red/20 text-neon-red text-xs font-bold flex items-center justify-center font-mono">
                      {idx + 1}
                    </span>
                    <div>
                      <p className="text-sm font-medium text-white">
                        {truncate(scene.description || scene.script_text || `Escena ${idx + 1}`, 80)}
                      </p>
                      {scene.duration_ms && (
                        <p className="text-xs text-gray-500">{formatDuration(scene.duration_ms / 1000)}</p>
                      )}
                    </div>
                  </div>
                  <div className="flex gap-1">
                    <button
                      onClick={() => openSceneEditor(scene)}
                      className="p-1.5 rounded hover:bg-dark-600 text-gray-400 hover:text-white transition-colors"
                      title="Editar texto"
                    >
                      <Edit3 size={14} />
                    </button>
                    <button
                      onClick={() => handleRegenerateAudio(scene.id)}
                      className="p-1.5 rounded hover:bg-dark-600 text-gray-400 hover:text-neon-cyan transition-colors"
                      title="Regenerar audio"
                    >
                      <Volume2 size={14} />
                    </button>
                    <button
                      onClick={() => handleReplaceImage(scene.id)}
                      className="p-1.5 rounded hover:bg-dark-600 text-gray-400 hover:text-neon-gold transition-colors"
                      title="Reemplazar imagen"
                    >
                      <Image size={14} />
                    </button>
                    <button
                      onClick={() => handleRegenerateAudio(scene.id)}
                      className="p-1.5 rounded hover:bg-dark-600 text-gray-400 hover:text-neon-cyan transition-colors"
                      title="Refrescar"
                    >
                      <RefreshCw size={14} />
                    </button>
                  </div>
                </div>

                {/* Inline editor */}
                {editingScene === scene.id && (
                  <div className="px-3 pb-3 animate-slide-up">
                    <textarea
                      value={editText}
                      onChange={e => setEditText(e.target.value)}
                      rows={4}
                      className="w-full px-3 py-2 bg-dark-800 border border-surface-border rounded-lg text-white text-sm focus:outline-none focus:border-neon-red resize-none font-mono"
                      autoFocus
                    />
                    <div className="flex gap-2 mt-2">
                      <button
                        onClick={() => handleSaveScene(scene.id)}
                        disabled={saving}
                        className="px-3 py-1.5 bg-neon-red text-white rounded-lg text-xs font-medium hover:bg-neon-red/80 disabled:opacity-50"
                      >
                        {saving ? 'Guardando...' : 'Guardar Escena'}
                      </button>
                      <button
                        onClick={() => setEditingScene(null)}
                        className="px-3 py-1.5 bg-dark-600 text-gray-300 rounded-lg text-xs hover:bg-dark-500"
                      >
                        Cancelar
                      </button>
                    </div>
                  </div>
                )}

                {/* Scene thumbnail */}
                {scene.image_path && (
                  <div className="px-3 pb-3">
                    <div className="w-full h-24 rounded-lg overflow-hidden bg-dark-800">
                      <img
                        src={mediaUrl(scene.image_path)}
                        alt={`Escena ${idx + 1}`}
                        className="w-full h-full object-cover opacity-60"
                      />
                    </div>
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </div>

      {/* YouTube Link */}
      {video.yt_url && (
        <div className="glass rounded-xl p-5 border border-purple-500/30">
          <p className="text-sm text-purple-300 flex items-center gap-2">
            <Play size={16} />
            Subido a YouTube:{' '}
            <a href={video.yt_url} target="_blank" rel="noopener noreferrer" className="text-neon-cyan hover:underline font-mono">
              {video.yt_video_id}
            </a>
          </p>
        </div>
      )}
    </div>
  )
}
