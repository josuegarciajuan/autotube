import { useEffect, useRef } from 'react'
import { useEasterEgg } from '../context/EasterEggContext'

interface PlanetChannel {
  id: number
  name: string
  slug: string
}

interface PlanetViewProps {
  channels: PlanetChannel[]
  mainChannel: PlanetChannel
}

export default function PlanetView({ channels, mainChannel }: PlanetViewProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const animRef = useRef<number>(0)
  const { matrixMode, partyMode } = useEasterEgg()

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const ctx = canvas.getContext('2d')
    if (!ctx) return

    const dpr = window.devicePixelRatio || 1
    const width = canvas.clientWidth
    const height = canvas.clientHeight
    canvas.width = width * dpr
    canvas.height = height * dpr
    ctx.scale(dpr, dpr)

    const cx = width / 2
    const cy = height / 2
    const allChannels = channels.length > 0 ? channels : [mainChannel]
    const planetCount = allChannels.length
    const colors = ['#ff3355', '#a855f7', '#00e5ff', '#22c55e', '#ffb830', '#ec4899']

    const ctx2d = ctx  // capture narrowed reference for closure
    let angle = 0

    function draw() {
      ctx2d.clearRect(0, 0, width, height)

      // Central sun
      const sunGradient = ctx2d.createRadialGradient(cx, cy, 0, cx, cy, 25)
      sunGradient.addColorStop(0, '#ffb830')
      sunGradient.addColorStop(0.5, '#ff335580')
      sunGradient.addColorStop(1, '#ff335500')
      ctx2d.beginPath()
      ctx2d.arc(cx, cy, 25, 0, Math.PI * 2)
      ctx2d.fillStyle = sunGradient
      ctx2d.fill()

      // Sun glow
      if (partyMode) {
        ctx2d.beginPath()
        ctx2d.arc(cx, cy, 30 + Math.sin(angle * 3) * 10, 0, Math.PI * 2)
        ctx2d.strokeStyle = `hsl(${(angle * 20) % 360}, 80%, 60%)`
        ctx2d.lineWidth = 2
        ctx2d.stroke()
      }

      // Orbiting planets
      const baseOrbit = 60
      const orbitSpacing = 45

      for (let i = 0; i < planetCount; i++) {
        const orbitRadius = baseOrbit + i * orbitSpacing
        const planetAngle = angle + (i * Math.PI * 2) / planetCount
        const px = cx + Math.cos(planetAngle) * orbitRadius
        const py = cy + Math.sin(planetAngle) * orbitRadius
        const planetSize = 8 + i * 2

        // Orbit line
        ctx2d.beginPath()
        ctx2d.arc(cx, cy, orbitRadius, 0, Math.PI * 2)
        ctx2d.strokeStyle = '#2a2a4a'
        ctx2d.lineWidth = 0.5
        ctx2d.stroke()

        // Planet
        const color = partyMode
          ? `hsl(${(i * 60 + angle * 15) % 360}, 80%, 60%)`
          : colors[i % colors.length]

        const planetGrad = ctx2d.createRadialGradient(px - 2, py - 2, 0, px, py, planetSize)
        planetGrad.addColorStop(0, color)
        planetGrad.addColorStop(1, `${color}00`)

        ctx2d.beginPath()
        ctx2d.arc(px, py, planetSize, 0, Math.PI * 2)
        ctx2d.fillStyle = planetGrad
        ctx2d.fill()
        ctx2d.strokeStyle = color
        ctx2d.lineWidth = 1
        ctx2d.stroke()

        // Glow
        if (matrixMode) {
          ctx2d.shadowColor = color
          ctx2d.shadowBlur = 15
          ctx2d.beginPath()
          ctx2d.arc(px, py, planetSize, 0, Math.PI * 2)
          ctx2d.fillStyle = `${color}40`
          ctx2d.fill()
          ctx2d.shadowBlur = 0
        }

        // Moon(s) orbiting
        for (let m = 0; m < 1 + i % 2; m++) {
          const moonAngle = angle * 4 + m * Math.PI
          const moonDist = planetSize + 6
          const mx = px + Math.cos(moonAngle) * moonDist
          const my = py + Math.sin(moonAngle) * moonDist
          ctx2d.beginPath()
          ctx2d.arc(mx, my, 2, 0, Math.PI * 2)
          ctx2d.fillStyle = color
          ctx2d.fill()
        }

        // Label
        ctx2d.font = '8px Inter, sans-serif'
        ctx2d.fillStyle = '#888'
        ctx2d.textAlign = 'center'
        const label = allChannels[i]?.name || ''
        ctx2d.fillText(label.length > 12 ? label.slice(0, 10) + '..' : label, px, py - planetSize - 6)
      }

      angle += 0.004
      animRef.current = requestAnimationFrame(draw)
    }

    draw()

    return () => cancelAnimationFrame(animRef.current)
  }, [channels, mainChannel, partyMode, matrixMode])

  return (
    <div className="glass rounded-xl p-4">
      <h3 className="text-sm font-semibold text-gray-300 mb-2">Vista Planetaria</h3>
      <p className="text-[10px] text-gray-600 mb-2">Orbitas: tamano = views, distancia = engagement, lunas = videos recientes</p>
      <canvas
        ref={canvasRef}
        className="w-full rounded-lg"
        style={{ height: '220px', background: '#0a0a12' }}
      />
      <div className="text-[9px] text-gray-600 mt-1 text-center">☀️ Total &nbsp;|&nbsp; 🌍 Canales orbitando</div>
    </div>
  )
}
