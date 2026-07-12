import { useState } from 'react';
import { ShieldAlert, LayoutPanelTop, CheckCircle2, Circle, ExternalLink, AlertTriangle } from 'lucide-react';
import { api } from '../lib/api';

interface ManualChecklistCardProps {
  videoId: number;
  channelId?: number;
  ytVideoId?: string | null;
  alteredContentDone?: number;
  endScreensDone?: number;
  onRefresh?: () => void;
  compact?: boolean;
}

interface ChecklistItem {
  id: string;
  label: string;
  icon: React.ReactNode;
  done: boolean;
}

export default function ManualChecklistCard({
  videoId,
  channelId,
  ytVideoId,
  alteredContentDone = 0,
  endScreensDone = 0,
  onRefresh,
  compact = false,
}: ManualChecklistCardProps) {
  const [updating, setUpdating] = useState<string | null>(null);

  const items: ChecklistItem[] = [
    {
      id: 'altered_content',
      label: 'Marcar contenido alterado/IA en Studio',
      icon: <ShieldAlert size={14} />,
      done: alteredContentDone === 1,
    },
    {
      id: 'end_screens',
      label: 'Configurar pantallas finales en Studio',
      icon: <LayoutPanelTop size={14} />,
      done: endScreensDone === 1,
    },
  ];

  const pendingCount = items.filter(i => !i.done).length;

  const handleToggle = async (itemId: string, currentDone: boolean) => {
    if (updating) return;
    setUpdating(itemId);
    try {
      await api.updateManualChecklist(videoId, itemId, !currentDone);
      onRefresh?.();
    } catch (e: any) {
      alert('Error: ' + (e?.message || 'No se pudo actualizar'));
    } finally {
      setUpdating(null);
    }
  };

  const studioUrl = 'https://studio.youtube.com';

  // Compact mode: just a mini alert pill
  if (compact && pendingCount > 0) {
    return (
      <div className="flex items-center gap-1.5 px-2 py-1 bg-neon-red/5 border border-neon-red/20 rounded-md">
        <AlertTriangle size={12} className="text-neon-red animate-pulse" />
        <span className="text-[10px] text-neon-red font-medium">
          {pendingCount} pendiente{pendingCount !== 1 ? 's' : ''}
        </span>
      </div>
    );
  }

  if (compact && pendingCount === 0) return null;

  // Full mode
  return (
    <div className="glass rounded-xl p-5 space-y-4 animate-slide-up">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold text-gray-300 flex items-center gap-2">
          <AlertTriangle size={16} className="text-neon-red" />
          Acciones Manuales Requeridas
        </h3>
        {pendingCount > 0 ? (
          <span className="text-xs px-2 py-0.5 bg-neon-red/10 text-neon-red rounded-full border border-neon-red/20">
            {pendingCount} pendiente{pendingCount !== 1 ? 's' : ''}
          </span>
        ) : (
          <span className="text-xs px-2 py-0.5 bg-emerald-500/10 text-emerald-400 rounded-full border border-emerald-500/20">
            Completado
          </span>
        )}
      </div>

      <p className="text-xs text-gray-500">
        Estas acciones no pueden automatizarse vía API. Debes realizarlas en YouTube Studio.
      </p>

      <div className="space-y-2">
        {items.map(item => (
          <div
            key={item.id}
            className={`flex items-center justify-between p-3 rounded-lg border transition-colors ${
              item.done
                ? 'border-emerald-500/20 bg-emerald-500/5'
                : 'border-surface-border bg-dark-700/50'
            }`}
          >
            <div className="flex items-center gap-3">
              <button
                onClick={() => handleToggle(item.id, item.done)}
                disabled={updating === item.id}
                className="text-gray-500 hover:text-neon-cyan transition-colors disabled:opacity-50"
              >
                {item.done ? (
                  <CheckCircle2 size={18} className="text-emerald-400" />
                ) : (
                  <Circle size={18} />
                )}
              </button>
              <span className={item.done ? 'text-gray-500 line-through text-sm' : 'text-gray-300 text-sm'}>
                {item.label}
              </span>
              {updating === item.id && (
                <span className="text-[10px] text-neon-cyan animate-pulse">Guardando...</span>
              )}
            </div>
            <span className={`text-xs ${item.done ? 'text-emerald-400' : 'text-gray-500'}`}>
              {item.done ? 'Completado' : 'Pendiente'}
            </span>
          </div>
        ))}
      </div>

      {pendingCount > 0 && (
        <a
          href={studioUrl}
          target="_blank"
          rel="noopener noreferrer"
          className="flex items-center gap-1.5 text-xs text-neon-cyan hover:text-white transition-colors"
        >
          <ExternalLink size={12} />
          Abrir YouTube Studio
        </a>
      )}
    </div>
  );
}
