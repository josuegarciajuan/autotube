import { useState, useEffect } from 'react'
import { api, formatDateTime } from '../lib/api'
import { Calendar, Clock, Plus, Trash2, Edit3, ToggleLeft, ToggleRight, Repeat, Target } from 'lucide-react'
import ExecutionTimeline from '../components/ExecutionTimeline'

export default function Scheduling() {
  const [schedules, setSchedules] = useState<any[]>([])
  const [channels, setChannels] = useState<any[]>([])
  const [loading, setLoading] = useState(true)
  const [showForm, setShowForm] = useState(false)
  const [editing, setEditing] = useState<any>(null)

  // Form state
  const [form, setForm] = useState({
    channel_id: 0,
    schedule_type: 'recurring',
    interval_h: 24,
    next_run_at: '',
  })

  useEffect(() => { loadAll() }, [])

  async function loadAll() {
    try {
      const [sch, chs] = await Promise.all([api.getSchedules(), api.getChannels(true)])
      setSchedules(sch)
      setChannels(chs)
    } catch (e) { console.error(e) }
    setLoading(false)
  }

  function openNew() {
    setEditing(null)
    const now = new Date()
    now.setHours(now.getHours() + 1)
    setForm({
      channel_id: channels[0]?.id || 0,
      schedule_type: 'recurring',
      interval_h: 24,
      next_run_at: now.toISOString().slice(0, 16),
    })
    setShowForm(true)
  }

  function openEdit(s: any) {
    setEditing(s)
    // Normalize next_run_at for datetime-local input: space → 'T'
    let nextRun = s.next_run_at?.slice(0, 16) || ''
    if (nextRun && nextRun.includes(' ')) nextRun = nextRun.replace(' ', 'T')
    setForm({
      channel_id: s.channel_id,
      schedule_type: s.schedule_type,
      interval_h: s.interval_h || 24,
      next_run_at: nextRun,
    })
    setShowForm(true)
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    try {
      const payload: any = {
        channel_id: form.channel_id,
        action: 'generate_and_upload',
        schedule_type: form.schedule_type,
        interval_h: form.interval_h,
        next_run_at: form.next_run_at ? form.next_run_at + ':00' : null,
      }

      if (editing) {
        await api.updateSchedule(editing.id, payload)
      } else {
        await api.createSchedule(payload)
      }
      setShowForm(false)
      loadAll()
    } catch (e: any) { alert(e.message) }
  }

  async function handleDelete(id: number) {
    if (!confirm('¿Eliminar esta programación?')) return
    try { await api.deleteSchedule(id); loadAll() } catch (e: any) { alert(e.message) }
  }

  async function handleToggle(s: any) {
    try { await api.toggleSchedule(s.id); loadAll() } catch (e: any) { alert(e.message) }
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="animate-spin rounded-full h-8 w-8 border-2 border-neon-red border-t-transparent" />
      </div>
    )
  }

  const recurring = schedules.filter(s => s.schedule_type === 'recurring')
  const oneTime = schedules.filter(s => s.schedule_type === 'one_time')

  return (
    <div className="max-w-6xl mx-auto space-y-6 animate-fade-in">
      {/* Header */}
      <div className="flex items-center justify-between">
        <h2 className="font-display text-2xl font-bold text-white flex items-center gap-3">
          <Calendar size={24} className="text-neon-gold" />
          Programación
        </h2>
        <button
          onClick={openNew}
          className="flex items-center gap-2 px-4 py-2 bg-neon-gold text-dark-900 rounded-lg hover:bg-neon-gold/80 transition-all text-sm font-bold"
        >
          <Plus size={16} /> Nueva Programación
        </button>
      </div>

      {/* Recurring */}
      <section className="glass rounded-xl p-5">
        <h3 className="font-display text-base font-semibold text-white mb-4 flex items-center gap-2">
          <Repeat size={16} className="text-neon-cyan" /> Programaciones Recurrentes
        </h3>
        {recurring.length === 0 ? (
          <p className="text-sm text-gray-500 py-4 text-center">No hay programaciones recurrentes</p>
        ) : (
          <div className="space-y-2">
            {recurring.map((s: any) => (
              <div key={s.id} className="flex items-center justify-between p-3 bg-dark-700/50 rounded-lg border border-surface-border">
                <div className="flex items-center gap-4">
                  <button onClick={() => handleToggle(s)} className="text-gray-400 hover:text-white">
                    {s.active ? <ToggleRight size={20} className="text-green-400" /> : <ToggleLeft size={20} className="text-gray-600" />}
                  </button>
                  <div>
                    <p className="text-sm font-medium text-white">
                      <Repeat size={12} className="inline mr-1 text-neon-cyan" />
                      Cada {s.interval_h}h · {s.channel_name}
                    </p>
                    <p className="text-xs text-gray-500">
                      Generar y Subir
                      {s.next_run_at && <span className="ml-2 text-neon-gold">Próxima: {formatDateTime(s.next_run_at)}</span>}
                    </p>
                  </div>
                </div>
                <div className="flex gap-1">
                  <button onClick={() => openEdit(s)} className="p-1.5 rounded hover:bg-dark-600 text-gray-400 hover:text-white"><Edit3 size={14} /></button>
                  <button onClick={() => handleDelete(s.id)} className="p-1.5 rounded hover:bg-red-900/30 text-gray-400 hover:text-red-400"><Trash2 size={14} /></button>
                </div>
              </div>
            ))}
          </div>
        )}
      </section>

      {/* One-time */}
      <section className="glass rounded-xl p-5">
        <h3 className="font-display text-base font-semibold text-white mb-4 flex items-center gap-2">
          <Target size={16} className="text-purple-400" /> Programaciones Puntuales
        </h3>
        {oneTime.length === 0 ? (
          <p className="text-sm text-gray-500 py-4 text-center">No hay programaciones puntuales</p>
        ) : (
          <div className="space-y-2">
            {oneTime.map((s: any) => (
              <div key={s.id} className="flex items-center justify-between p-3 bg-dark-700/50 rounded-lg border border-surface-border">
                <div className="flex items-center gap-4">
                  <button onClick={() => handleToggle(s)} className="text-gray-400 hover:text-white">
                    {s.active ? <ToggleRight size={20} className="text-green-400" /> : <ToggleLeft size={20} className="text-gray-600" />}
                  </button>
                  <div>
                    <p className="text-sm font-medium text-white">
                      <Target size={12} className="inline mr-1 text-purple-400" />
                      {s.channel_name}
                    </p>
                    <p className="text-xs text-gray-500">
                      Generar y Subir
                      {s.next_run_at && <span className="ml-2 text-neon-gold">Próxima: {formatDateTime(s.next_run_at)}</span>}
                    </p>
                  </div>
                </div>
                <div className="flex gap-1">
                  <button onClick={() => openEdit(s)} className="p-1.5 rounded hover:bg-dark-600 text-gray-400 hover:text-white"><Edit3 size={14} /></button>
                  <button onClick={() => handleDelete(s.id)} className="p-1.5 rounded hover:bg-red-900/30 text-gray-400 hover:text-red-400"><Trash2 size={14} /></button>
                </div>
              </div>
            ))}
          </div>
        )}
      </section>

      {/* Execution Timeline — unified view: past, running, future */}
      <ExecutionTimeline />

      {/* Form Modal */}
      {showForm && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm" onClick={() => setShowForm(false)}>
          <div className="glass rounded-xl p-6 w-full max-w-md space-y-4 animate-slide-up max-h-[90vh] overflow-y-auto" onClick={e => e.stopPropagation()}>
            <h3 className="font-display text-lg font-semibold text-white">
              {editing ? 'Editar Programación' : 'Nueva Programación'}
            </h3>
            <form onSubmit={handleSubmit} className="space-y-3">
              <div>
                <label className="block text-xs text-gray-400 mb-1">Canal</label>
                <select value={form.channel_id} onChange={e => setForm({ ...form, channel_id: Number(e.target.value) })}
                  className="w-full px-3 py-2 bg-dark-700 border border-surface-border rounded-lg text-white text-sm focus:outline-none focus:border-neon-gold">
                  {channels.map((ch: any) => <option key={ch.id} value={ch.id}>{ch.name}</option>)}
                </select>
              </div>

              <div>
                <label className="block text-xs text-gray-400 mb-1">Tipo</label>
                <div className="flex gap-2">
                  <button type="button" onClick={() => setForm({ ...form, schedule_type: 'recurring' })}
                    className={`flex-1 px-3 py-2 rounded-lg text-sm font-medium border transition-all ${form.schedule_type === 'recurring' ? 'bg-neon-cyan/10 border-neon-cyan text-neon-cyan' : 'border-surface-border text-gray-400 hover:border-gray-600'}`}>
                    <Repeat size={14} className="inline mr-1" /> Recurrente
                  </button>
                  <button type="button" onClick={() => setForm({ ...form, schedule_type: 'one_time' })}
                    className={`flex-1 px-3 py-2 rounded-lg text-sm font-medium border transition-all ${form.schedule_type === 'one_time' ? 'bg-purple-500/10 border-purple-500 text-purple-400' : 'border-surface-border text-gray-400 hover:border-gray-600'}`}>
                    <Target size={14} className="inline mr-1" /> Puntual
                  </button>
                </div>
              </div>

              {form.schedule_type === 'recurring' && (
                <div>
                  <label className="block text-xs text-gray-400 mb-1">Intervalo (horas)</label>
                  <input type="number" value={form.interval_h} onChange={e => setForm({ ...form, interval_h: Number(e.target.value) })}
                    min={1} max={720}
                    className="w-full px-3 py-2 bg-dark-700 border border-surface-border rounded-lg text-white text-sm focus:outline-none focus:border-neon-gold" />
                </div>
              )}

              <div>
                <label className="block text-xs text-gray-400 mb-1">Primera ejecución</label>
                <input type="datetime-local" value={form.next_run_at} onChange={e => setForm({ ...form, next_run_at: e.target.value })}
                  className="w-full px-3 py-2 bg-dark-700 border border-surface-border rounded-lg text-white text-sm focus:outline-none focus:border-neon-gold" />
              </div>

              <div className="flex gap-2 pt-2">
                <button type="submit" className="flex-1 px-4 py-2 bg-neon-gold text-dark-900 rounded-lg font-bold text-sm hover:bg-neon-gold/80">
                  {editing ? 'Guardar' : 'Crear'}
                </button>
                <button type="button" onClick={() => setShowForm(false)} className="px-4 py-2 bg-dark-600 text-gray-300 rounded-lg text-sm hover:bg-dark-500">Cancelar</button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  )
}
