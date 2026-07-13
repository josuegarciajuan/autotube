import { useState } from 'react'
import { Heart, Eye, Zap, Clock, Cog } from 'lucide-react'
import { AreaChart, Area, ResponsiveContainer, XAxis, YAxis, Tooltip } from 'recharts'
import { motion, AnimatePresence } from 'framer-motion'
import { useEasterEgg } from '../context/EasterEggContext'
import { formatShortNumber } from '../lib/api'

interface KPI {
  key: string
  label: string
  value: number
  delta: number | null
  icon: typeof Heart
  color: string
  breakdown?: { longform: number; shorts: number }
}

interface VitalSignsBarProps {
  kpis: KPI[]
  sparklines: Record<string, number[]>
  channelBreakdown?: Array<{ name: string; slug: string; value: number }>
}

// Build sparkline data for Recharts
function buildSparkData(values: number[]) {
  return values.map((v, i) => ({ day: i, value: v }))
}

export default function VitalSignsBar({ kpis, sparklines, channelBreakdown }: VitalSignsBarProps) {
  const [expandedKey, setExpandedKey] = useState<string | null>(null)
  const { triggerGlitch, glitchTick } = useEasterEgg()

  const toggleExpand = (key: string) => {
    setExpandedKey(prev => prev === key ? null : key)
    triggerGlitch()
  }

  // Count-up animation target
  const CountUpValue = ({ value }: { value: number }) => (
    <motion.span
      key={`${value}-${glitchTick}`}
      initial={{ opacity: 0, y: 20 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.6, ease: 'easeOut' }}
      className="font-mono tabular-nums"
    >
      {formatShortNumber(value)}
    </motion.span>
  )

  const DeltaBadge = ({ delta, color }: { delta: number | null; color: string }) => {
    if (delta === null) return null
    const isUp = delta >= 0
    const badgeColor = delta > 5 ? '#22c55e' : delta < -5 ? '#ff3355' : '#ffb830'
    return (
      <span className="text-xs ml-1" style={{ color: badgeColor }}>
        {isUp ? '↑' : '↓'}{Math.abs(delta)}%
      </span>
    )
  }

  return (
    <div className="mb-6">
      {/* Scanline overlay */}
      <div className="relative">
        {/* KPI Row */}
        <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-5 gap-3"
          style={{
            background: 'linear-gradient(180deg, rgba(0,229,255,0.02) 0%, rgba(255,51,85,0.02) 100%)',
            borderRadius: '12px',
            border: '1px solid rgba(42,42,74,0.5)',
            padding: '12px',
            position: 'relative',
            overflow: 'hidden',
          }}>
          {/* Scanline animation */}
          <div className="absolute inset-0 pointer-events-none" style={{
            background: 'linear-gradient(180deg, transparent 50%, rgba(255,51,85,0.03) 50%)',
            backgroundSize: '100% 4px',
            animation: 'scanline 3s linear infinite',
            zIndex: 0,
          }} />

          {kpis.map(kpi => {
            const isExpanded = expandedKey === kpi.key
            const Icon = kpi.icon
            return (
              <button
                key={kpi.key}
                onClick={() => toggleExpand(kpi.key)}
                className={`relative z-10 flex flex-col items-center gap-2 p-3 rounded-xl border transition-all duration-300 text-left w-full
                  ${isExpanded
                    ? 'border-neon-red/60 bg-dark-800 shadow-[0_0_20px_rgba(255,51,85,0.15)]'
                    : 'border-transparent hover:border-dark-500 hover:bg-dark-700/50'
                  }`}
              >
                <div className="flex items-center gap-2">
                  <Icon
                    className={`w-5 h-5 transition-all ${isExpanded ? 'animate-pulse' : ''}`}
                    style={{ color: kpi.color }}
                  />
                  <span className="text-xs text-gray-500 uppercase tracking-wider">{kpi.label}</span>
                </div>
                <div className="flex items-baseline gap-1">
                  <span className={`text-xl font-bold font-mono ${isExpanded ? 'text-neon-red' : 'text-white'}`}>
                    <CountUpValue value={kpi.value} />
                  </span>
                  <DeltaBadge delta={kpi.delta} color={kpi.color} />
                </div>
                {/* Mini sparkline */}
                <div className="w-full h-8 opacity-50">
                  <ResponsiveContainer width="100%" height="100%">
                    <AreaChart data={buildSparkData(sparklines[kpi.key] || [])}>
                      <Area type="monotone" dataKey="value" stroke={kpi.color} fill={kpi.color} fillOpacity={0.15} strokeWidth={1} dot={false} />
                    </AreaChart>
                  </ResponsiveContainer>
                </div>
              </button>
            )
          })}
        </div>

        {/* Expanded panel */}
        <AnimatePresence>
          {expandedKey && (() => {
            const kpi = kpis.find(k => k.key === expandedKey)
            if (!kpi) return null
            const data = buildSparkData(sparklines[kpi.key] || [])
            return (
              <motion.div
                initial={{ height: 0, opacity: 0 }}
                animate={{ height: 'auto', opacity: 1 }}
                exit={{ height: 0, opacity: 0 }}
                transition={{ duration: 0.4, ease: 'easeInOut' }}
                className="overflow-hidden"
              >
                <div className="mt-3 p-4 rounded-xl border border-dark-500 bg-dark-800/80"
                  style={{ backdropFilter: 'blur(8px)' }}>
                  <h4 className="text-sm font-semibold text-gray-300 mb-3">{kpi.label} - Ultimos 30 dias</h4>
                  <ResponsiveContainer width="100%" height={160}>
                    <AreaChart data={data}>
                      <defs>
                        <linearGradient id={`grad-${kpi.key}`} x1="0" y1="0" x2="0" y2="1">
                          <stop offset="0%" stopColor={kpi.color} stopOpacity={0.4} />
                          <stop offset="100%" stopColor={kpi.color} stopOpacity={0.02} />
                        </linearGradient>
                      </defs>
                      <XAxis dataKey="day" hide />
                      <YAxis hide />
                      <Tooltip
                        contentStyle={{
                          background: 'rgba(15,15,22,0.95)',
                          border: '1px solid rgba(42,42,74,0.5)',
                          borderRadius: '8px',
                          fontSize: '12px',
                        }}
                        labelFormatter={() => ''}
                      />
                      <Area type="monotone" dataKey="value" stroke={kpi.color} strokeWidth={2}
                        fill={`url(#grad-${kpi.key})`} dot={false} />
                    </AreaChart>
                  </ResponsiveContainer>

                  {/* Channel breakdown */}
                  {channelBreakdown && channelBreakdown.length > 0 && (
                    <div className="mt-3 space-y-2">
                      <span className="text-xs text-gray-500">Breakdown por canal</span>
                      {channelBreakdown.map(ch => {
                        const pct = kpi.value > 0 ? Math.round(ch.value / kpi.value * 100) : 0
                        return (
                          <div key={ch.slug} className="flex items-center gap-2 text-xs">
                            <span className="text-gray-400 w-24 truncate">{ch.name}</span>
                            <div className="flex-1 h-2 bg-dark-600 rounded-full overflow-hidden">
                              <div className="h-full rounded-full transition-all duration-500"
                                style={{ width: `${pct}%`, background: kpi.color }} />
                            </div>
                            <span className="text-gray-500 font-mono w-10 text-right">{pct}%</span>
                          </div>
                        )
                      })}
                    </div>
                  )}
                </div>
              </motion.div>
            )
          })()}
        </AnimatePresence>
      </div>

      {/* Inject scanline keyframe */}
      <style>{`
        @keyframes scanline {
          0% { transform: translateY(-100%); }
          100% { transform: translateY(100%); }
        }
      `}</style>
    </div>
  )
}
