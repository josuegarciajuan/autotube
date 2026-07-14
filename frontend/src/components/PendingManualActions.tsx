import { useEffect, useState } from 'react';
import { AlertTriangle, CheckCircle2 } from 'lucide-react';
import { api, type PendingManualSummary } from '../lib/api';

export default function PendingManualActions() {
  const [data, setData] = useState<PendingManualSummary | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    loadData();
    const interval = setInterval(loadData, 120000);
    return () => clearInterval(interval);
  }, []);

  const loadData = async () => {
    try {
      const result = await api.getPendingManualActions();
      setData(result);
    } catch {
      // silent
    } finally {
      setLoading(false);
    }
  };

  if (loading) return null;
  if (!data || data.total_pending === 0) return null;

  const channelEntries = Object.values(data.channels);

  return (
    <div className="glass rounded-xl p-5 animate-slide-up">
      <h3 className="text-sm font-semibold text-gray-300 mb-4 flex items-center gap-2">
        <AlertTriangle size={16} className="text-neon-red" />
        Acciones Manuales Pendientes
        <span className="text-xs text-neon-red bg-neon-red/10 px-2 py-0.5 rounded-full border border-neon-red/20 ml-1">
          {data.total_pending}
        </span>
      </h3>

      <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-3">
        {channelEntries.map((ch) => (
          <a
            key={ch.channel_id}
            href={`#/channels/${ch.channel_id}`}
            className="flex flex-col p-3 rounded-lg bg-dark-700/40 border border-surface-border/50 hover:border-neon-red/30 transition-colors"
          >
            <div className="text-xs text-gray-300 font-medium truncate mb-2">
              {ch.channel_name}
            </div>
            <div className="flex items-center gap-2">
              <span className="text-lg font-bold text-neon-red tabular-nums">
                {ch.total_pending}
              </span>
              <span className="text-[10px] text-gray-500">
                {ch.affected_videos} vídeo{ch.affected_videos !== 1 ? 's' : ''}
              </span>
            </div>
            <div className="flex gap-2 mt-1.5 text-[10px]">
              {ch.pending_altered > 0 && (
                <span className="text-neon-red/60">+{ch.pending_altered} cont. alterado</span>
              )}
              {ch.pending_endscreens > 0 && (
                <span className="text-neon-red/60">+{ch.pending_endscreens} pant. final</span>
              )}
            </div>
          </a>
        ))}
      </div>
    </div>
  );
}
