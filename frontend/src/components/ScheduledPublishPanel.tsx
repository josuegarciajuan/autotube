import { useEffect, useState } from 'react';
import { Clock, Zap, Hash, ListPlus } from 'lucide-react';
import { api, statusBadge, statusLabel, formatCountdown, formatTargetTime, formatDateTime } from '../lib/api';

interface ScheduledPublishPanelProps {
  videoId: number;
  onRefresh?: () => void;
}

export default function ScheduledPublishPanel({ videoId, onRefresh }: ScheduledPublishPanelProps) {
  const [video, setVideo] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  const [countdown, setCountdown] = useState('');
  const [publishing, setPublishing] = useState(false);
  const [cancelling, setCancelling] = useState(false);

  useEffect(() => {
    loadVideo();
    const interval = setInterval(loadVideo, 30000); // refresh every 30s
    return () => clearInterval(interval);
  }, [videoId]);

  useEffect(() => {
    if (!video?.target_public_at) return;
    const tick = () => setCountdown(formatCountdown(video.target_public_at));
    tick();
    const interval = setInterval(tick, 60000);
    return () => clearInterval(interval);
  }, [video?.target_public_at]);

  const loadVideo = async () => {
    try {
      setLoading(true);
      const v = await api.getVideo(videoId);
      setVideo(v);
    } catch (e) {
      // ignore
    } finally {
      setLoading(false);
    }
  };

  const handlePublishNow = async () => {
    if (!confirm('¿Publicar este vídeo ahora mismo? Se omitirá la hora programada.')) return;
    setPublishing(true);
    try {
      await api.publishNow(videoId);
      loadVideo();
      onRefresh?.();
    } catch (e: any) {
      alert('Error: ' + (e?.message || 'No se pudo publicar'));
    } finally {
      setPublishing(false);
    }
  };

  const handleCancel = async () => {
    if (!confirm('¿Cancelar la publicación programada? El vídeo se quedará como no listado.')) return;
    setCancelling(true);
    try {
      await api.cancelSchedule(videoId);
      loadVideo();
      onRefresh?.();
    } catch (e: any) {
      alert('Error: ' + (e?.message || 'No se pudo cancelar'));
    } finally {
      setCancelling(false);
    }
  };

  if (loading) return <div className="text-gray-500 text-sm py-4">Cargando...</div>;
  if (!video) return <div className="text-red-400 text-sm py-4">No se pudo cargar el vídeo</div>;

  const isScheduled = ['uploaded_private', 'warming', 'scheduled'].includes(video.status);
  const isPublished = video.status === 'published';
  const isPastSchedule = !isScheduled && !isPublished;

  // Check if video has no scheduled info at all
  if (!isScheduled && !isPublished && !video.target_public_at) {
    return null; // Not a scheduled video
  }

  return (
    <div className="glass rounded-xl p-5 space-y-4 animate-slide-up">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold text-gray-300 flex items-center gap-2">
          <Clock size={16} className="text-neon-gold" />
          Publicación Programada
        </h3>
        <span className={`badge ${statusBadge(video.status)}`}>
          {statusLabel(video.status)}
        </span>
      </div>

      <div className="space-y-3 text-sm">
        {/* Uploaded info */}
        {video.uploaded_at && (
          <div className="flex items-center gap-2 text-gray-400">
            <span className="w-2 h-2 rounded-full bg-cyan-500" />
            Subido como no listado: {formatDateTime(video.uploaded_at)}
          </div>
        )}

        {/* Warmup / waiting */}
        {isScheduled && video.target_public_at && !video.held && (
          <>
            <div className="flex items-center gap-2 text-amber-300">
              <span className={`w-2 h-2 rounded-full ${video.status === 'warming' ? 'animate-pulse' : ''}`} />
              <span>
                {video.status === 'warming'
                  ? 'Calentando — se publica en:'
                  : 'Programado — se publica en:'}
              </span>
              <span className="font-mono tabular-nums text-neon-gold font-bold">{countdown}</span>
            </div>

            {video.status === 'warming' && (
              <div className="w-full bg-dark-700 rounded-full h-1.5 overflow-hidden">
                <div className="h-full bg-amber-500 progress-shimmer rounded-full" style={{ width: '60%' }} />
              </div>
            )}
          </>
        )}

        {/* Retenido (hold por cuota agotada) — privado sin programar */}
        {video.held && (
          <div className="flex items-center gap-2 text-slate-400">
            <span className="w-2 h-2 rounded-full bg-slate-500" />
            <span>
              Privado (retenido) — se reprograma al resetear la cuota
            </span>
          </div>
        )}

        {/* Target time details */}
        {video.target_public_at && (
          <div className="flex items-center gap-2 text-gray-400">
            <Clock size={12} />
            Hora objetivo: {formatTargetTime(video.target_public_at)}
            {video.peak_source && (
              <span className="text-[10px] px-1.5 py-0.5 rounded bg-dark-600 text-gray-500">
                {video.peak_source === 'history' ? 'histórico' : 'heurística'}
              </span>
            )}
          </div>
        )}

        {/* Published info */}
        {isPublished && video.published_at && (
          <div className="flex items-center gap-2 text-emerald-400">
            <span className="w-2 h-2 rounded-full bg-emerald-500" />
            Publicado: {formatDateTime(video.published_at)}
          </div>
        )}

        {/* Playlist assignment */}
        {video.auto_playlist_name && (
          <div className="flex items-center gap-2 text-purple-400/80">
            <ListPlus size={12} />
            Playlist: {video.auto_playlist_name}
          </div>
        )}
      </div>

      {/* Actions */}
      {isScheduled && (
        <div className="flex gap-2 pt-2 border-t border-surface-border">
          <button
            onClick={handlePublishNow}
            disabled={publishing}
            className="flex-1 px-3 py-2 text-xs bg-emerald-600/20 hover:bg-emerald-600/30 text-emerald-300 border border-emerald-500/30 rounded-lg transition-colors disabled:opacity-50 flex items-center justify-center gap-1"
          >
            <Zap size={12} />
            {publishing ? 'Publicando...' : 'Publicar ahora'}
          </button>
          <button
            onClick={handleCancel}
            disabled={cancelling}
            className="px-3 py-2 text-xs bg-red-600/20 hover:bg-red-600/30 text-red-400 border border-red-500/30 rounded-lg transition-colors disabled:opacity-50"
          >
            {cancelling ? 'Cancelando...' : 'Cancelar prog.'}
          </button>
        </div>
      )}
    </div>
  );
}
