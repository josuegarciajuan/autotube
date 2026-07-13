import { useState } from 'react'
import { AreaChart, Area, ResponsiveContainer, XAxis, YAxis, Tooltip } from 'recharts'
import { motion, AnimatePresence } from 'framer-motion'
import { useEasterEgg } from '../context/EasterEggContext'
import { formatShortNumber } from '../lib/api'

interface HeatmapDay {
  date: string
  total_views: number
  channels: Record<string, number>
}

interface ActivityHeatmapProps {
  data: HeatmapDay[]
  channelSlugs: Record<string, string>  // channel_id -> slug
  channelNames: Record<string, string>   // channel_id -> name
  channelColors: Record<string, string>  // channel_id -> color
}

const CHANNEL_COLORS = ['#ff3355', '#a855f7', '#00e5ff', '#22c55e', '#ffb830', '#ec4899']

export default function ActivityHeatmap({ data, channelSlugs, channelNames, channelColors }: ActivityHeatmapProps) {
  const [expanded, setExpanded] = useState(false)
  const [showShorts, setShowShorts] = useState(false)
  const [psychedelic, setPsychedelic] = useState(false)
  const { triggerGlitch, matrixMode } = useEasterEgg()

  if (!data || data.length === 0) return null

  const maxViews = Math.max(...data.map(d => d.total_views), 1)
  const channelIds = Object.keys(channelSlugs)

  // Build streamgraph data: one entry per day with stacked channel values
  const streamData = data.map(d => {
    const entry: any = { date: d.date.slice(5) } // MM-DD
    channelIds.forEach(cid => {
      entry[cid] = d.channels[cid] || 0
    })
    return entry
  })

  // Calculate max stacked value for domain
  const maxStacked = Math.max(...streamData.map(d =>
    channelIds.reduce((sum, cid) => sum + (d[cid] || 0), 0)
  ), 1)

  const toggleExpand = () => {
    setExpanded(prev => !prev)
    triggerGlitch()
  }

  return (
    <div className="mb-6">
      {/* Collapsed: mini heatmap */}
      <button
        onClick={toggleExpand}
        className={`w-full text-left p-4 rounded-xl border transition-all duration-300
          ${expanded ? 'border-neon-red/50 bg-dark-800' : 'border-dark-500 bg-dark-800/60 hover:border-gray-400'}`}
      >
        <div className="flex items-center justify-between mb-3">
          <div className="flex items-center gap-2">
            <span className="text-lg">🔥</span>
            <h3 className="text-sm font-semibold text-gray-300">
              Actividad — Ultimos {data.length} dias
            </h3>
          </div>
          <motion.span
            animate={{ rotate: expanded ? 180 : 0 }}
            className="text-gray-500 text-xs"
          >
            ▼
          </motion.span>
        </div>

        {/* Mini heatmap grid — GitHub style */}
        <div className="flex gap-[2px] flex-wrap justify-end">
          {data.slice(-21).map((day, i) => {
            const intensity = day.total_views / maxViews
            const hue = psychedelic ? (i * 15) % 360 : 0
            const saturation = psychedelic ? 80 : 0
            return (
              <div
                key={i}
                className="w-3 h-3 rounded-sm transition-all duration-300"
                style={{
                  backgroundColor: psychedelic
                    ? `hsl(${hue}, ${saturation}%, ${Math.round(intensity * 60 + 20)}%)`
                    : `rgba(255, 51, 85, ${Math.round(intensity * 0.6 + 0.05)})`,
                  opacity: day.total_views === 0 ? 0.15 : 1,
                  boxShadow: intensity > 0.7 ? `0 0 4px rgba(255,51,85,${intensity})` : 'none',
                  filter: matrixMode ? 'hue-rotate(180deg)' : 'none',
                }}
                title={`${day.date}: ${formatShortNumber(day.total_views)} views`}
                onDoubleClick={() => setPsychedelic(p => !p)}
              />
            )
          })}
        </div>

        {/* Peak / Valley */}
        {data.length > 0 && (
          <div className="flex justify-between mt-2 text-[11px] text-gray-600 font-mono">
            <span>Pico: {data.reduce((a, b) => a.total_views > b.total_views ? a : b).date.slice(5)} - {formatShortNumber(Math.max(...data.map(d => d.total_views)))}</span>
            <span>Valle: {data.reduce((a, b) => a.total_views < b.total_views ? a : b).date.slice(5)}</span>
          </div>
        )}
      </button>

      {/* Expanded: streamgraph + toggle */}
      <AnimatePresence>
        {expanded && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.4 }}
            className="overflow-hidden"
          >
            <div className="mt-3 p-4 rounded-xl border border-dark-500 bg-dark-800/80">
              {/* Toggle longform/shorts */}
              <div className="flex items-center gap-2 mb-4">
                {[
                  { label: 'Longform', value: false },
                  { label: 'Shorts', value: true },
                ].map(opt => (
                  <button
                    key={opt.label}
                    onClick={() => setShowShorts(opt.value)}
                    className={`px-3 py-1 rounded-full text-xs transition-all ${
                      showShorts === opt.value
                        ? 'bg-neon-red/20 text-neon-red border border-neon-red/40'
                        : 'text-gray-500 hover:text-gray-300 border border-transparent'
                    }`}
                  >
                    {opt.label}
                  </button>
                ))}
                <button
                  onClick={() => { setPsychedelic(p => !p); triggerGlitch() }}
                  className={`px-3 py-1 rounded-full text-xs transition-all ml-auto ${
                    psychedelic ? 'bg-neon-purple/20 text-neon-purple border border-neon-purple/40' : 'text-gray-600 hover:text-gray-400'
                  }`}
                  title="Modo psicodelico"
                >
                  🌀
                </button>
              </div>

              {/* Streamgraph */}
              <ResponsiveContainer width="100%" height={200}>
                <AreaChart data={streamData}>
                  <defs>
                    {channelIds.map((cid, i) => (
                      <linearGradient key={cid} id={`stream-${cid}`} x1="0" y1="0" x2="0" y2="1">
                        <stop offset="0%" stopColor={channelColors[cid] || CHANNEL_COLORS[i]} stopOpacity={0.8} />
                        <stop offset="100%" stopColor={channelColors[cid] || CHANNEL_COLORS[i]} stopOpacity={0.05} />
                      </linearGradient>
                    ))}
                  </defs>
                  <XAxis dataKey="date" tick={{ fontSize: 10, fill: '#666' }} />
                  <YAxis hide domain={[0, maxStacked]} />
                  <Tooltip
                    contentStyle={{ background: 'rgba(15,15,22,0.95)', border: '1px solid rgba(42,42,74,0.5)', borderRadius: '8px', fontSize: '11px' }}
                  />
                  {channelIds.map((cid, i) => (
                    <Area
                      key={cid}
                      type="monotone"
                      dataKey={cid}
                      stackId="1"
                      stroke={channelColors[cid] || CHANNEL_COLORS[i]}
                      fill={`url(#stream-${cid})`}
                      strokeWidth={1}
                      dot={false}
                      isAnimationActive={true}
                    />
                  ))}
                </AreaChart>
              </ResponsiveContainer>

              {/* Legend */}
              <div className="flex gap-3 mt-2 flex-wrap">
                {channelIds.map((cid, i) => (
                  <div key={cid} className="flex items-center gap-1 text-[10px] text-gray-500">
                    <span className="w-2 h-2 rounded-full" style={{ backgroundColor: channelColors[cid] || CHANNEL_COLORS[i] }} />
                    {channelNames[cid] || cid}
                  </div>
                ))}
              </div>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}
