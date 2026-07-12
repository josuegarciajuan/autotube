import { useState } from 'react';
import { Clock, Zap, AlertCircle } from 'lucide-react';
import { api } from '../lib/api';

interface PublicationModeToggleProps {
  channelId: number;
  currentMode: string;
  onToggle?: (newMode: string) => void;
}

export default function PublicationModeToggle({ channelId, currentMode, onToggle }: PublicationModeToggleProps) {
  const [mode, setMode] = useState(currentMode);
  const [showConfirm, setShowConfirm] = useState(false);
  const [toggling, setToggling] = useState(false);

  const isImmediate = mode === 'immediate';

  const handleToggle = () => {
    if (isImmediate) {
      // Show confirmation when enabling scheduled mode
      setShowConfirm(true);
    } else {
      // Disable directly
      executeToggle();
    }
  };

  const executeToggle = async () => {
    setToggling(true);
    try {
      const result = await api.toggleScheduledMode(channelId);
      setMode(result.publish_mode);
      onToggle?.(result.publish_mode);
    } catch (e: any) {
      alert('Error: ' + (e?.message || 'No se pudo cambiar el modo'));
    } finally {
      setToggling(false);
      setShowConfirm(false);
    }
  };

  return (
    <>
      <button
        onClick={handleToggle}
        disabled={toggling}
        className={`flex items-center gap-1.5 px-3 py-2 rounded-lg text-xs sm:text-sm font-medium transition-colors ${
          isImmediate
            ? 'bg-dark-600 border border-surface-border text-gray-500 hover:text-gray-300'
            : 'bg-neon-gold/10 border border-neon-gold/30 text-neon-gold'
        } disabled:opacity-50`}
        title={isImmediate ? 'Activar publicación programada' : 'Volver a modo inmediato'}
      >
        {isImmediate ? <Zap size={14} /> : <Clock size={14} />}
        <span className="hidden sm:inline">
          {isImmediate ? 'Modo: Inmediato' : 'Modo: Programado'}
        </span>
        <span className="sm:hidden">
          {isImmediate ? 'Inmediato' : 'Programado'}
        </span>
      </button>

      {/* Confirmation modal */}
      {showConfirm && (
        <div className="fixed inset-0 z-[9999] flex items-center justify-center bg-black/70">
          <div className="bg-dark-800 border border-surface-border rounded-2xl p-6 max-w-md w-full mx-4 shadow-2xl animate-slide-up">
            <div className="flex items-center gap-3 mb-4">
              <div className="w-10 h-10 rounded-xl bg-neon-gold/10 flex items-center justify-center">
                <Clock size={20} className="text-neon-gold" />
              </div>
              <h2 className="text-lg font-semibold text-white">Activar Publicación Programada</h2>
            </div>

            <div className="space-y-3 text-sm text-gray-400 mb-6">
              <div className="flex items-start gap-2">
                <AlertCircle size={14} className="text-neon-gold mt-0.5 flex-shrink-0" />
                <p>Los vídeos de este canal se subirán como <strong className="text-white">privados</strong> en lugar de públicos.</p>
              </div>
              <div className="flex items-start gap-2">
                <Clock size={14} className="text-neon-gold mt-0.5 flex-shrink-0" />
                <p>Permanecerán en <strong className="text-white">calentamiento</strong> (≈20 min) y se publicarán automáticamente a la <strong className="text-white">hora pico calculada</strong>.</p>
              </div>
              <div className="flex items-start gap-2">
                <Zap size={14} className="text-neon-gold mt-0.5 flex-shrink-0" />
                <p>El modo inmediato queda desactivado. Podrás revertirlo en cualquier momento.</p>
              </div>
            </div>

            <div className="flex gap-2">
              <button
                onClick={() => setShowConfirm(false)}
                className="flex-1 px-4 py-2.5 text-sm bg-dark-600 hover:bg-dark-500 text-gray-300 rounded-lg transition-colors"
              >
                Cancelar
              </button>
              <button
                onClick={executeToggle}
                disabled={toggling}
                className="flex-1 px-4 py-2.5 text-sm bg-neon-gold/20 hover:bg-neon-gold/30 text-neon-gold border border-neon-gold/30 rounded-lg transition-colors disabled:opacity-50"
              >
                {toggling ? 'Activando...' : 'Activar modo programado'}
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
