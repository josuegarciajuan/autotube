import { useState, useEffect } from 'react'
import { motion } from 'framer-motion'
import { api, formatShortNumber } from '../lib/api'

interface FlowNode {
  name: string
  value: number
}

interface SankeyFlowProps {
  channelId: number
}

export default function SankeyFlow({ channelId }: SankeyFlowProps) {
  const [flowData, setFlowData] = useState<{ nodes: FlowNode[]; links: any[] } | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    api.getContentFlow(channelId)
      .then(setFlowData)
      .catch(() => {})
      .finally(() => setLoading(false))
  }, [channelId])

  if (loading) return null
  if (!flowData || flowData.nodes.length === 0) return (
    <div className="glass rounded-xl p-4">
      <h3 className="text-sm font-semibold text-gray-300 mb-2">Flujo de Contenido</h3>
      <p className="text-xs text-gray-600">Sin datos de flujo</p>
    </div>
  )

  const nodes = flowData.nodes
  const maxVal = Math.max(...nodes.map(n => n.value), 1)
  const COLORS = ['#ff3355', '#a855f7', '#00e5ff', '#ffb830', '#22c55e']

  return (
    <div className="glass rounded-xl p-4">
      <h3 className="text-sm font-semibold text-gray-300 mb-3">Flujo de Contenido</h3>
      <p className="text-[10px] text-gray-600 mb-3">Topicos → Scripts → Videos → Views → Revenue</p>

      {/* Horizontal flow visualization */}
      <div className="relative flex items-center justify-between h-32 px-4">
        {nodes.map((node, i) => {
          const width = Math.max(30, (node.value / maxVal) * 100)
          // Flow connectors between nodes
          const nextNode = nodes[i + 1]
          return (
            <div key={i} className="flex-1 flex flex-col items-center relative">
              {/* Node */}
              <motion.div
                initial={{ scale: 0, opacity: 0 }}
                animate={{ scale: 1, opacity: 1 }}
                transition={{ delay: i * 0.15 }}
                className="z-10 flex flex-col items-center"
              >
                <motion.div
                  animate={{ boxShadow: [`0 0 10px ${COLORS[i]}40`, `0 0 20px ${COLORS[i]}60`, `0 0 10px ${COLORS[i]}40`] }}
                  transition={{ repeat: Infinity, duration: 3, delay: i * 0.5 }}
                  className="rounded-2xl px-4 py-2 text-center border"
                  style={{
                    backgroundColor: `${COLORS[i]}15`,
                    borderColor: `${COLORS[i]}40`,
                    minWidth: `${width}px`,
                  } as React.CSSProperties}
                >
                  <span className="text-[10px] font-medium text-gray-400 block">{node.name}</span>
                  <span className="text-sm font-bold font-mono" style={{ color: COLORS[i] }}>
                    {formatShortNumber(node.value)}
                  </span>
                </motion.div>
              </motion.div>

              {/* Flow arrow to next */}
              {nextNode && (
                <div className="absolute top-1/2 left-full w-full h-0.5 -translate-y-1/2 z-0"
                  style={{
                    background: `linear-gradient(90deg, ${COLORS[i]}, ${COLORS[i + 1]})`,
                  }}
                >
                  <motion.div
                    animate={{ x: ['0%', '100%'] }}
                    transition={{ repeat: Infinity, duration: 2, delay: i * 0.3 }}
                    className="absolute top-1/2 -translate-y-1/2 w-2 h-2 rounded-full"
                    style={{ backgroundColor: COLORS[i] } as React.CSSProperties}
                  />
                </div>
              )}
            </div>
          )
        })}
      </div>

      {/* Conversion rates */}
      <div className="flex justify-between text-[9px] text-gray-600 mt-2">
        {flowData.links.map((link: any, i: number) => {
          const sourceVal = nodes[link.source]?.value || 1
          const rate = link.value > 0 ? Math.round(link.value / sourceVal * 100) : 0
          return (
            <div key={i} className={`flex-1 text-center ${i === 0 ? '' : ''}`}>
              {rate > 0 && <span>{rate}% conversion</span>}
            </div>
          )
        })}
      </div>
    </div>
  )
}
