import { useEffect, useState } from 'react';
import { Clock, ChevronRight } from 'lucide-react';
import { api, statusBadge, statusLabel, formatCountdown, formatTargetTime, type UpcomingPublication } from '../lib/api';

export default function UpcomingPublications() {
  const [publications, setPublications] = useState<UpcomingPublication[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    loadData();
    const interval = setInterval(loadData, 60000);
    return () => clearInterval(interval);
  }, []);

  const loadData = async () => {
    try {
      const data = await api.getUpcomingPublications(undefined, 2);
      setPublications(data);
    } catch {
      // silent
    } finally {
      setLoading(false);
    }
  };

  if (loading) return null;

  const today = new Date().toISOString().slice(0, 10);
  const todayPubs = publications.filter(p => p.target_public_at?.startsWith(today));
  const futurePubs = publications.filter(p => !p.target_public_at?.startsWith(today) || new Date(p.target_public_at!) > new Date(today + 'T23:59:59'));

  if (publications.length === 0) return null;

  return (
    <div className="glass rounded-xl p-5 animate-slide-up">
      <h3 className="text-sm font-semibold text-gray-300 mb-4 flex items-center gap-2">
        <Clock size={16} className="text-neon-gold" />
        Próximas Publicaciones
        <span className="text-xs text-gray-500 font-normal ml-1">
          ({publications.length} programada{publications.length !== 1 ? 's' : ''})
        </span>
      </h3>

      <div className="space-y-2">
        {publications.slice(0, 5).map((pub) => (
          <a
            key={pub.video_id}
            href={`#/videos/${pub.video_id}/edit`}
            className="flex items-center gap-3 p-3 rounded-lg bg-dark-700/30 hover:bg-dark-600/50 border border-surface-border/50 transition-colors group"
          >
            {/* Time column */}
            <div className="w-14 flex-shrink-0">
              <div className="text-xs font-mono tabular-nums text-neon-gold font-bold">
                {formatTargetTime(pub.target_public_at)}
              </div>
              <div className="text-[10px] text-gray-500 font-mono tabular-nums">
                {formatCountdown(pub.target_public_at)}
              </div>
            </div>

            {/* Status dot */}
            <div className={`w-2 h-2 rounded-full flex-shrink-0 ${
              pub.status === 'warming' ? 'bg-amber-500 animate-pulse' :
              pub.status === 'scheduled' ? 'bg-yellow-500' :
              'bg-cyan-500'
            }`} />

            {/* Content */}
            <div className="flex-1 min-w-0">
              <div className="text-xs text-gray-300 truncate">{pub.titulo_final || 'Sin título'}</div>
              <div className="flex items-center gap-2 mt-0.5">
                <span className="text-[10px] text-neon-purple/70">
                  {pub.channel_name}
                </span>
                {pub.auto_playlist_name && (
                  <span className="text-[10px] text-gray-600">
                    ▶ {pub.auto_playlist_name}
                  </span>
                )}
              </div>
            </div>

            {/* Badge */}
            <span className={`badge ${statusBadge(pub.status)} text-[10px] flex-shrink-0`}>
              {statusLabel(pub.status)}
            </span>

            {/* Manual pending indicator */}
            {(pub.pending_altered || pub.pending_endscreens) && (
              <span className="text-[10px] text-neon-red bg-neon-red/10 px-1.5 py-0.5 rounded-full flex-shrink-0">
                {(pub.pending_altered + pub.pending_endscreens)}
              </span>
            )}

            <ChevronRight size={12} className="text-gray-600 group-hover:text-white transition-colors flex-shrink-0" />
          </a>
        ))}
      </div>
    </div>
  );
}
