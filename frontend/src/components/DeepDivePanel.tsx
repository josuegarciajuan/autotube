import { useState, useEffect } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { X, TrendingUp, FileText, BarChart3, FlaskConical } from 'lucide-react'
import { AreaChart, Area, BarChart, Bar, LineChart, Line, RadarChart, Radar, PolarGrid, PolarAngleAxis, PolarRadiusAxis, ResponsiveContainer, XAxis, YAxis, Tooltip, CartesianGrid } from 'recharts'
import { api, formatShortNumber } from '../lib/api'
import BadgeGallery from './BadgeGallery'
import Streaks from './Streaks'
import BossFight from './BossFight'
import PlanetView from './PlanetView'
import SankeyFlow from './SankeyFlow'
import MatrixRain from './MatrixRain'

interface DeepDivePanelProps {
  channel: { id: number; name: string; slug: string } | null
  open: boolean
  onClose: () => void
  comparisonChannels?: Array<{ id: number; name: string; slug: string }>
}

type TabKey = 'crecimiento' | 'contenido' | 'comparativa' | 'alquimia'

const TABS: { key: TabKey; label: string; icon: typeof TrendingUp }[] = [
  { key: 'crecimiento', label: 'Crecimiento', icon: TrendingUp },
  { key: 'contenido', label: 'Contenido', icon: FileText },
  { key: 'comparativa', label: 'Comparativa', icon: BarChart3 },
  { key: 'alquimia', label: 'Alquimia', icon: FlaskConical },
]

export default function DeepDivePanel({ channel, open, onClose, comparisonChannels = [] }: DeepDivePanelProps) {
  const [activeTab, setActiveTab] = useState<TabKey>('crecimiento')
  const [growthData, setGrowthData] = useState<any>(null)
  const [contentRanking, setContentRanking] = useState<any[]>([])
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    if (!channel || !open) return
    setLoading(true)

    Promise.all([
      api.getChannelGrowth(channel.id, 90).catch(() => null),
      api.getChannelContentRanking(channel.id, 'views', 20).catch(() => []),
    ]).then(([growth, ranking]) => {
      setGrowthData(growth)
      setContentRanking(ranking)
    }).finally(() => setLoading(false))
  }, [channel, open])

  if (!channel) return null

  return (
    <AnimatePresence>
      {open && (
        <>
          {/* Backdrop */}
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            className="fixed inset-0 bg-black/60 z-40"
            onClick={onClose}
          />
          {/* Panel */}
          <motion.div
            initial={{ x: '100%' }}
            animate={{ x: 0 }}
            exit={{ x: '100%' }}
            transition={{ type: 'spring', damping: 25, stiffness: 200 }}
            className="fixed top-0 right-0 h-full w-full max-w-3xl z-50 overflow-y-auto"
            style={{
              background: 'linear-gradient(180deg, #0f0f1e 0%, #0a0a12 100%)',
              borderLeft: '1px solid rgba(42,42,74,0.5)',
              scrollbarWidth: 'thin',
              scrollbarColor: '#2a2a4a transparent',
            } as React.CSSProperties}
          >
            {/* Header */}
            <div className="sticky top-0 z-10 flex items-center justify-between p-4 border-b border-dark-500"
              style={{ background: 'rgba(15,15,30,0.95)', backdropFilter: 'blur(12px)' }}>
              <div className="flex items-center gap-3">
                <div className="w-9 h-9 rounded-full overflow-hidden border-2 border-neon-red/50">
                  <img
                    src={`api/static/output/thumbnails/${channel.slug}/avatar.jpg`}
                    alt={channel.name}
                    className="w-full h-full object-cover"
                    onError={(e) => { (e.target as HTMLImageElement).style.display = 'none' }}
                  />
                </div>
                <div>
                  <h2 className="text-sm font-bold text-white">{channel.name}</h2>
                  <span className="text-[10px] text-gray-500 font-mono">DEEP DIVE</span>
                </div>
              </div>
              <button onClick={onClose} className="p-2 rounded-lg hover:bg-dark-600 text-gray-400 hover:text-white transition-colors">
                <X className="w-5 h-5" />
              </button>
            </div>

            {/* Tabs */}
            <div className="flex border-b border-dark-500">
              {TABS.map(tab => {
                const Icon = tab.icon
                const active = activeTab === tab.key
                return (
                  <button
                    key={tab.key}
                    onClick={() => setActiveTab(tab.key)}
                    className={`flex items-center gap-2 px-4 py-3 text-xs font-medium transition-all border-b-2 flex-1 justify-center
                      ${active ? 'border-neon-red text-neon-red bg-neon-red/5' : 'border-transparent text-gray-500 hover:text-gray-300'}`}
                  >
                    <Icon className="w-4 h-4" />
                    {tab.label}
                  </button>
                )
              })}
            </div>

            {/* Tab content */}
            <div className="p-4 space-y-6">
              {loading && (
                <div className="flex items-center justify-center py-12">
                  <div className="animate-spin w-6 h-6 border-2 border-neon-red/30 border-t-neon-red rounded-full" />
                </div>
              )}

              {/* CRECIMIENTO */}
              {activeTab === 'crecimiento' && growthData && (
                <div className="space-y-6">
                  <div className="glass rounded-xl p-4">
                    <h3 className="text-sm font-semibold text-gray-300 mb-3">Subs + Views (90d)</h3>
                    <ResponsiveContainer width="100%" height={200}>
                      <AreaChart data={growthData.daily || []}>
                        <defs>
                          <linearGradient id="dv-views" x1="0" y1="0" x2="0" y2="1">
                            <stop offset="0%" stopColor="#ff3355" stopOpacity={0.3} />
                            <stop offset="100%" stopColor="#ff3355" stopOpacity={0} />
                          </linearGradient>
                          <linearGradient id="dv-subs" x1="0" y1="0" x2="0" y2="1">
                            <stop offset="0%" stopColor="#00e5ff" stopOpacity={0.3} />
                            <stop offset="100%" stopColor="#00e5ff" stopOpacity={0} />
                          </linearGradient>
                        </defs>
                        <CartesianGrid strokeDasharray="3 3" stroke="#1a1a2e" />
                        <XAxis dataKey="date" tick={{ fontSize: 10, fill: '#666' }} />
                        <YAxis yAxisId="views" tick={{ fontSize: 10, fill: '#666' }} tickFormatter={v => formatShortNumber(v)} />
                        <YAxis yAxisId="subs" orientation="right" tick={{ fontSize: 10, fill: '#00e5ff' }} tickFormatter={v => formatShortNumber(v)} />
                        <Tooltip contentStyle={{ background: '#0f0f16', border: '1px solid #2a2a4a', borderRadius: '8px', fontSize: '11px' }}
                          formatter={(v: any) => formatShortNumber(v)} />
                        <Area yAxisId="views" type="monotone" dataKey="views" stroke="#ff3355" fill="url(#dv-views)" strokeWidth={2} dot={false} />
                        <Area yAxisId="subs" type="monotone" dataKey="subscribers" stroke="#00e5ff" fill="url(#dv-subs)" strokeWidth={2} dot={false} />
                      </AreaChart>
                    </ResponsiveContainer>
                  </div>

                  {/* Streaks for this channel */}
                  <Streaks channelId={channel.id} />

                  {/* Boss fights */}
                  <BossFight channelId={channel.id} channelName={channel.name} />
                </div>
              )}

              {/* CONTENIDO */}
              {activeTab === 'contenido' && (
                <div className="space-y-4">
                  <h3 className="text-sm font-semibold text-gray-300">Top Contenido</h3>
                  {contentRanking.length === 0 && !loading && (
                    <p className="text-xs text-gray-600">Sin datos de contenido</p>
                  )}
                  <div className="space-y-2">
                    {contentRanking.slice(0, 10).map((v: any, i: number) => (
                      <motion.div
                        key={v.id || i}
                        initial={{ opacity: 0, x: -20 }}
                        animate={{ opacity: 1, x: 0 }}
                        transition={{ delay: i * 0.05 }}
                        className="flex items-center gap-3 p-3 rounded-lg border border-dark-500 bg-dark-800/60 hover:border-neon-red/30 transition-all"
                      >
                        <span className={`font-mono text-sm font-bold w-6 ${i < 3 ? 'text-neon-gold' : 'text-gray-500'}`}>
                          #{i + 1}
                        </span>
                        <div className="flex-1 min-w-0">
                          <p className="text-xs text-gray-300 truncate">{v.titulo_final || v.title || 'Sin titulo'}</p>
                          <p className="text-[10px] text-gray-600 font-mono">{formatShortNumber(v.views || 0)} views</p>
                        </div>
                        <div className="flex-1 hidden sm:block">
                          <div className="h-1.5 bg-dark-600 rounded-full overflow-hidden">
                            <motion.div
                              initial={{ width: 0 }}
                              animate={{ width: `${Math.min(100, ((v.views || 0) / Math.max(...contentRanking.map((x: any) => x.views || 0), 1)) * 100)}%` }}
                              className="h-full bg-neon-red rounded-full"
                              transition={{ duration: 1, delay: i * 0.05 }}
                            />
                          </div>
                        </div>
                      </motion.div>
                    ))}
                  </div>
                  {/* Badge gallery */}
                  <BadgeGallery channelId={channel.id} />
                </div>
              )}

              {/* COMPARATIVA */}
              {activeTab === 'comparativa' && (
                <div className="space-y-6">
                  <div className="glass rounded-xl p-4">
                    <h3 className="text-sm font-semibold text-gray-300 mb-3">Radar Comparativo</h3>
                    <ResponsiveContainer width="100%" height={300}>
                      <RadarChart data={[
                        { metric: 'Views', ...Object.fromEntries(comparisonChannels.map(c => [c.slug, 80])) },
                        { metric: 'Subs', ...Object.fromEntries(comparisonChannels.map(c => [c.slug, 60])) },
                        { metric: 'Engage', ...Object.fromEntries(comparisonChannels.map(c => [c.slug, 45])) },
                        { metric: 'Watch H', ...Object.fromEntries(comparisonChannels.map(c => [c.slug, 70])) },
                        { metric: 'Likes', ...Object.fromEntries(comparisonChannels.map(c => [c.slug, 55])) },
                        { metric: 'Revenue', ...Object.fromEntries(comparisonChannels.map(c => [c.slug, 30])) },
                      ]}>
                        <PolarGrid stroke="#1a1a2e" />
                        <PolarAngleAxis dataKey="metric" tick={{ fontSize: 10, fill: '#666' }} />
                        <PolarRadiusAxis tick={{ fontSize: 9, fill: '#555' }} />
                        {comparisonChannels.map((c, i) => {
                          const colors = ['#ff3355', '#a855f7', '#00e5ff', '#22c55e']
                          return (
                            <Radar key={c.slug} name={c.name} dataKey={c.slug} stroke={colors[i]} fill={colors[i]} fillOpacity={0.15} strokeWidth={2} />
                          )
                        })}
                      </RadarChart>
                    </ResponsiveContainer>
                  </div>
                </div>
              )}

              {/* ALQUIMIA */}
              {activeTab === 'alquimia' && (
                <div className="space-y-6">
                  <PlanetView channels={comparisonChannels} mainChannel={channel} />
                  <SankeyFlow channelId={channel.id} />
                  <MatrixRain channelId={channel.id} />
                </div>
              )}
            </div>
          </motion.div>
        </>
      )}
    </AnimatePresence>
  )
}
