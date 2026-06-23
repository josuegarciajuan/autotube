import { useState, useEffect } from 'react'
import { useSearchParams, Link } from 'react-router-dom'
import { api, formatDate, formatDateTime, truncate } from '../lib/api'
import { FileText, Plus, Trash2, Calendar, Edit3, Play, Clock, Filter, ChevronDown, ExternalLink } from 'lucide-react'

export default function Content() {
  const [searchParams] = useSearchParams()
  const preselectedChannel = searchParams.get('channel')

  const [channels, setChannels] = useState<any[]>([])
  const [selectedChannel, setSelectedChannel] = useState<any>(null)
  const [content, setContent] = useState<any[]>([])
  const [scripts, setScripts] = useState<any[]>([])
  const [loading, setLoading] = useState(true)
  const [tab, setTab] = useState<'content' | 'scripts' | 'scheduled'>('content')
  
  // Modals
  const [showCreate, setShowCreate] = useState(false)
  const [showSchedule, setShowSchedule] = useState<number | null>(null)
  const [scheduleDate, setScheduleDate] = useState('')
  const [scheduleTime, setScheduleTime] = useState('10:00')
  const [editContent, setEditContent] = useState<any>(null)
  
  // Create form
  const [newTitle, setNewTitle] = useState('')
  const [newText, setNewText] = useState('')
  const [newSource, setNewSource] = useState('manual')

  // Load channels
  useEffect(() => {
    api.getChannels(true).then(chs => {
      setChannels(chs)
      // Preselect channel from query param
      if (preselectedChannel) {
        const found = chs.find((c: any) => c.id === Number(preselectedChannel))
        if (found) setSelectedChannel(found)
      } else if (chs.length > 0) {
        setSelectedChannel(chs[0])
      }
    })
  }, [])

  // Load content when channel changes
  useEffect(() => {
    if (!selectedChannel) return
    loadData()
  }, [selectedChannel, tab])

  async function loadData() {
    setLoading(true)
    try {
      if (tab === 'content') {
        const data = await api.getContent(undefined, selectedChannel.id, false)
        setContent(data)
      } else if (tab === 'scheduled') {
        const data = await api.getContent(undefined, selectedChannel.id, false, 'scheduled')
        setContent(data)
      } else {
        const data = await api.getScripts(selectedChannel.slug)
        setScripts(data)
      }
    } catch (e) { console.error(e) }
    setLoading(false)
  }

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault()
    if (!newTitle.trim() || !newText.trim()) return
    try {
      await api.createContent({
        title: newTitle,
        text: newText,
        source: newSource,
        canal: selectedChannel.slug,
      })
      setShowCreate(false)
      setNewTitle('')
      setNewText('')
      setNewSource('manual')
      loadData()
    } catch (e: any) { alert(e.message) }
  }

  async function handleDelete(id: number) {
    if (!confirm('¿Eliminar este contenido?')) return
    try {
      await api.deleteContent(id)
      loadData()
    } catch (e: any) { alert(e.message) }
  }

  async function handleSchedule(id: number) {
    const iso = `${scheduleDate}T${scheduleTime}:00`
    try {
      await api.scheduleContent(id, iso)
      setShowSchedule(null)
      loadData()
    } catch (e: any) { alert(e.message) }
  }

  async function handleUpdateContent() {
    if (!editContent) return
    try {
      await api.updateContent(editContent.id, {
        title: editContent.title,
        text: editContent.text,
        source: editContent.source,
      })
      setEditContent(null)
      loadData()
    } catch (e: any) { alert(e.message) }
  }

  async function generateScriptFromContent(contentId: number) {
    try {
      await api.getContent() // placeholder
      alert('Generando guion... (implementación pendiente de conectar al pipeline)')
    } catch {}
  }

  if (loading && !selectedChannel) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="animate-spin rounded-full h-8 w-8 border-2 border-neon-red border-t-transparent" />
      </div>
    )
  }

  function statusBadgeContent(status: string) {
    const map: Record<string, string> = {
      pending: 'badge bg-yellow-900/40 text-yellow-300 border border-yellow-500/30',
      ready: 'badge bg-green-900/40 text-green-300 border border-green-500/30',
      scheduled: 'badge bg-blue-900/40 text-blue-300 border border-blue-500/30',
      done: 'badge bg-purple-900/40 text-purple-300 border border-purple-500/30',
    }
    return map[status] || 'badge bg-gray-700 text-gray-400'
  }

  function statusLabelContent(status: string) {
    const map: Record<string, string> = { pending: 'Pendiente', ready: 'Listo', scheduled: 'Programado', done: 'Hecho' }
    return map[status] || status
  }

  return (
    <div className="max-w-6xl mx-auto space-y-5 animate-fade-in">
      {/* Header */}
      <div className="flex items-center justify-between flex-wrap gap-3">
        <h2 className="font-display text-2xl font-bold text-white flex items-center gap-3">
          <FileText size={24} className="text-neon-cyan" />
          Contenido
        </h2>
        <div className="flex items-center gap-3">
          {/* Channel selector */}
          <div className="relative">
            <select
              value={selectedChannel?.id || ''}
              onChange={e => {
                const ch = channels.find(c => c.id === Number(e.target.value))
                setSelectedChannel(ch)
              }}
              className="appearance-none pl-3 pr-8 py-2 bg-dark-700 border border-surface-border rounded-lg text-white text-sm focus:outline-none focus:border-neon-cyan cursor-pointer"
            >
              {channels.map((ch: any) => (
                <option key={ch.id} value={ch.id}>{ch.name}</option>
              ))}
            </select>
            <ChevronDown size={14} className="absolute right-2.5 top-3 text-gray-500 pointer-events-none" />
          </div>
          <button
            onClick={() => setShowCreate(true)}
            className="flex items-center gap-2 px-4 py-2 bg-neon-cyan text-dark-900 rounded-lg hover:bg-neon-cyan/80 transition-all text-sm font-bold"
          >
            <Plus size={16} /> Nuevo
          </button>
        </div>
      </div>

      {/* Tabs */}
      <div className="flex bg-dark-700 rounded-lg p-1 w-fit">
        {[
          { key: 'content', label: 'Fuentes' },
          { key: 'scripts', label: 'Guiones' },
          { key: 'scheduled', label: 'Programado' },
        ].map(t => (
          <button
            key={t.key}
            onClick={() => setTab(t.key as any)}
            className={`px-3 py-1.5 rounded text-sm font-medium transition-all ${
              tab === t.key ? 'bg-neon-red text-white' : 'text-gray-400 hover:text-white'
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>

      {/* Content List */}
      {loading ? (
        <div className="flex items-center justify-center h-32">
          <div className="animate-spin rounded-full h-6 w-6 border-2 border-neon-red border-t-transparent" />
        </div>
      ) : tab !== 'scripts' ? (
        content.length === 0 ? (
          <div className="text-center py-16 glass rounded-xl">
            <FileText size={48} className="mx-auto mb-4 opacity-20 text-gray-600" />
            <p className="text-gray-500">No hay contenido en {selectedChannel?.name}</p>
            <button onClick={() => setShowCreate(true)} className="mt-3 px-4 py-2 bg-neon-cyan text-dark-900 rounded-lg text-sm font-bold hover:bg-neon-cyan/80">
              Crear primer contenido
            </button>
          </div>
        ) : (
          <div className="space-y-2">
            {content.map((item: any) => (
              <div key={item.id} className="glass rounded-xl border border-surface-border hover:border-neon-cyan/30 transition-all group">
                <div className="p-4">
                  <div className="flex items-start justify-between">
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2 mb-1.5 flex-wrap">
                        <span className="px-2 py-0.5 rounded bg-dark-600 text-xs text-neon-cyan font-medium">
                          {item.source}
                        </span>
                        {item.subreddit && (
                          <span className="px-2 py-0.5 rounded bg-dark-600 text-xs text-gray-400">r/{item.subreddit}</span>
                        )}
                        <span className={statusBadgeContent(item.status || 'pending')}>
                          {statusLabelContent(item.status || 'pending')}
                        </span>
                        {item.scheduled_at && (
                          <span className="text-xs text-blue-300 flex items-center gap-1">
                            <Calendar size={11} /> {formatDateTime(item.scheduled_at)}
                          </span>
                        )}
                        {item.score > 0 && (
                          <span className="text-xs text-neon-gold font-mono">↑{item.score}</span>
                        )}
                      </div>
                      <p className="text-sm font-medium text-white">{item.title}</p>
                      <p className="text-xs text-gray-500 mt-1">{truncate(item.text, 200)}</p>
                    </div>
                  </div>
                  
                  {/* Action buttons */}
                  <div className="flex items-center gap-2 mt-3 pt-2 border-t border-surface-border">
                    <button
                      onClick={() => generateScriptFromContent(item.id)}
                      className="flex items-center gap-1 px-2.5 py-1 rounded text-xs text-neon-cyan hover:bg-neon-cyan/10 transition-colors"
                    >
                      <Play size={12} /> Guion
                    </button>
                    <button
                      onClick={() => {
                        setEditContent({ ...item })
                      }}
                      className="flex items-center gap-1 px-2.5 py-1 rounded text-xs text-gray-400 hover:text-white hover:bg-dark-600 transition-colors"
                    >
                      <Edit3 size={12} /> Editar
                    </button>
                    <button
                      onClick={() => { setShowSchedule(item.id); setScheduleDate(''); setScheduleTime('10:00') }}
                      className="flex items-center gap-1 px-2.5 py-1 rounded text-xs text-blue-400 hover:bg-blue-900/20 transition-colors"
                    >
                      <Calendar size={12} /> Programar
                    </button>
                    {item.url && (
                      <a href={item.url} target="_blank" rel="noopener noreferrer"
                        className="flex items-center gap-1 px-2.5 py-1 rounded text-xs text-gray-500 hover:text-gray-300 transition-colors">
                        <ExternalLink size={12} />
                      </a>
                    )}
                    <button
                      onClick={() => handleDelete(item.id)}
                      className="flex items-center gap-1 px-2.5 py-1 rounded text-xs text-gray-500 hover:text-red-400 hover:bg-red-900/20 transition-colors ml-auto"
                    >
                      <Trash2 size={12} />
                    </button>
                  </div>
                </div>
              </div>
            ))}
          </div>
        )
      ) : (
        /* Scripts tab */
        scripts.length === 0 ? (
          <div className="text-center py-16 glass rounded-xl">
            <FileText size={48} className="mx-auto mb-4 opacity-20 text-gray-600" />
            <p className="text-gray-500">No hay guiones en {selectedChannel?.name}</p>
          </div>
        ) : (
          <div className="space-y-2">
            {scripts.map((s: any) => (
              <div key={s.id} className="glass rounded-xl p-4 border border-surface-border hover:border-neon-red/30 transition-all">
                <div className="flex items-center justify-between mb-2">
                  <div className="flex items-center gap-2">
                    <span className={`px-2 py-0.5 rounded text-xs font-medium ${s.used ? 'bg-green-900/30 text-green-400' : 'bg-dark-600 text-gray-400'}`}>
                      {s.used ? 'Usado' : 'Disponible'}
                    </span>
                    {s.duracion_estimada && <span className="text-xs text-gray-500">{s.duracion_estimada} min</span>}
                  </div>
                  <span className="text-xs text-gray-600">{formatDate(s.created_at)}</span>
                </div>
                <p className="text-sm font-medium text-white">
                  {(() => { try { return JSON.parse(s.titulo_options || '[]')[0] || 'Sin título' } catch { return 'Sin título' } })()}
                </p>
                <p className="text-xs text-gray-500 mt-1">{truncate(s.guion || '', 150)}</p>
              </div>
            ))}
          </div>
        )
      )}

      {/* Create Modal */}
      {showCreate && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm" onClick={() => setShowCreate(false)}>
          <div className="glass rounded-xl p-6 w-full max-w-lg space-y-4 animate-slide-up" onClick={e => e.stopPropagation()}>
            <h3 className="font-display text-lg font-semibold text-white">Nuevo Contenido</h3>
            <form onSubmit={handleCreate} className="space-y-3">
              <div>
                <label className="block text-xs text-gray-400 mb-1">Título</label>
                <input type="text" value={newTitle} onChange={e => setNewTitle(e.target.value)}
                  className="w-full px-3 py-2 bg-dark-700 border border-surface-border rounded-lg text-white text-sm focus:outline-none focus:border-neon-cyan" autoFocus />
              </div>
              <div>
                <label className="block text-xs text-gray-400 mb-1">Texto</label>
                <textarea value={newText} onChange={e => setNewText(e.target.value)} rows={6}
                  className="w-full px-3 py-2 bg-dark-700 border border-surface-border rounded-lg text-white text-sm focus:outline-none focus:border-neon-cyan resize-none" />
              </div>
              <div>
                <label className="block text-xs text-gray-400 mb-1">Fuente</label>
                <select value={newSource} onChange={e => setNewSource(e.target.value)}
                  className="w-full px-3 py-2 bg-dark-700 border border-surface-border rounded-lg text-white text-sm focus:outline-none focus:border-neon-cyan">
                  <option value="manual">Manual</option>
                  <option value="reddit">Reddit</option>
                  <option value="wikipedia">Wikipedia</option>
                </select>
              </div>
              <div className="flex gap-2 pt-2">
                <button type="submit" className="flex-1 px-4 py-2 bg-neon-cyan text-dark-900 rounded-lg font-bold text-sm hover:bg-neon-cyan/80">Crear</button>
                <button type="button" onClick={() => setShowCreate(false)} className="px-4 py-2 bg-dark-600 text-gray-300 rounded-lg text-sm hover:bg-dark-500">Cancelar</button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* Edit Modal */}
      {editContent && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm" onClick={() => setEditContent(null)}>
          <div className="glass rounded-xl p-6 w-full max-w-lg space-y-4 animate-slide-up" onClick={e => e.stopPropagation()}>
            <h3 className="font-display text-lg font-semibold text-white">Editar Contenido</h3>
            <div className="space-y-3">
              <div>
                <label className="block text-xs text-gray-400 mb-1">Título</label>
                <input type="text" value={editContent.title} onChange={e => setEditContent({ ...editContent, title: e.target.value })}
                  className="w-full px-3 py-2 bg-dark-700 border border-surface-border rounded-lg text-white text-sm focus:outline-none focus:border-neon-cyan" />
              </div>
              <div>
                <label className="block text-xs text-gray-400 mb-1">Texto</label>
                <textarea value={editContent.text || ''} onChange={e => setEditContent({ ...editContent, text: e.target.value })} rows={6}
                  className="w-full px-3 py-2 bg-dark-700 border border-surface-border rounded-lg text-white text-sm focus:outline-none focus:border-neon-cyan resize-none" />
              </div>
              <div>
                <label className="block text-xs text-gray-400 mb-1">Fuente</label>
                <input type="text" value={editContent.source || ''} onChange={e => setEditContent({ ...editContent, source: e.target.value })}
                  className="w-full px-3 py-2 bg-dark-700 border border-surface-border rounded-lg text-white text-sm focus:outline-none focus:border-neon-cyan" />
              </div>
              <div className="flex gap-2 pt-2">
                <button onClick={handleUpdateContent} className="flex-1 px-4 py-2 bg-neon-cyan text-dark-900 rounded-lg font-bold text-sm hover:bg-neon-cyan/80">Guardar</button>
                <button onClick={() => setEditContent(null)} className="px-4 py-2 bg-dark-600 text-gray-300 rounded-lg text-sm hover:bg-dark-500">Cancelar</button>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Schedule Modal */}
      {showSchedule && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm" onClick={() => setShowSchedule(null)}>
          <div className="glass rounded-xl p-6 w-full max-w-sm space-y-4 animate-slide-up" onClick={e => e.stopPropagation()}>
            <h3 className="font-display text-lg font-semibold text-white flex items-center gap-2">
              <Calendar size={18} className="text-blue-400" /> Programar
            </h3>
            <div className="space-y-3">
              <div>
                <label className="block text-xs text-gray-400 mb-1">Fecha</label>
                <input type="date" value={scheduleDate} onChange={e => setScheduleDate(e.target.value)}
                  className="w-full px-3 py-2 bg-dark-700 border border-surface-border rounded-lg text-white text-sm focus:outline-none focus:border-blue-400" />
              </div>
              <div>
                <label className="block text-xs text-gray-400 mb-1">Hora</label>
                <input type="time" value={scheduleTime} onChange={e => setScheduleTime(e.target.value)}
                  className="w-full px-3 py-2 bg-dark-700 border border-surface-border rounded-lg text-white text-sm focus:outline-none focus:border-blue-400" />
              </div>
              <div className="flex gap-2 pt-2">
                <button onClick={() => handleSchedule(showSchedule)} className="flex-1 px-4 py-2 bg-blue-500 text-white rounded-lg font-bold text-sm hover:bg-blue-600">Programar</button>
                <button onClick={() => setShowSchedule(null)} className="px-4 py-2 bg-dark-600 text-gray-300 rounded-lg text-sm hover:bg-dark-500">Cancelar</button>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
