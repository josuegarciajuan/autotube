import { useEffect, useRef } from 'react'
import { useEasterEgg } from '../context/EasterEggContext'

interface MatrixRainProps {
  channelId: number
}

export default function MatrixRain({ channelId }: MatrixRainProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const animRef = useRef<number>(0)
  const { matrixMode } = useEasterEgg()

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

    const ctx2d = ctx  // capture narrowed reference
    const fontSize = 12
    const columns = Math.floor(width / fontSize)

    // Initialize drops
    const chars = '0123456789ABCDEF'.split('')
    const drops: number[] = []
    for (let i = 0; i < columns; i++) {
      drops[i] = Math.random() * -height
    }

    function draw() {
      ctx2d.fillStyle = 'rgba(0, 10, 0, 0.08)'
      ctx2d.fillRect(0, 0, width, height)

      ctx2d.font = `${fontSize}px "JetBrains Mono", monospace`

      for (let i = 0; i < drops.length; i++) {
        const char = chars[Math.floor(Math.random() * chars.length)]
        const x = i * fontSize
        const y = drops[i] * fontSize

        // Leading character brighter
        ctx2d.fillStyle = '#0f0'
        ctx2d.fillText(char, x, y)

        // Trailing chars dimmer
        ctx2d.fillStyle = '#005500'
        ctx2d.fillText(chars[Math.floor(Math.random() * chars.length)], x, y - fontSize)

        if (y > height && Math.random() > 0.975) {
          drops[i] = 0
        }
        drops[i]++
      }

      animRef.current = requestAnimationFrame(draw)
    }

    draw()

    return () => cancelAnimationFrame(animRef.current)
  }, [channelId])

  return (
    <div className="glass rounded-xl p-4">
      <h3 className="text-sm font-semibold text-gray-300 mb-2">Matrix de Vistas</h3>
      <p className="text-[10px] text-gray-600 mb-2">
        {matrixMode ? 'MODO MATRIX ACTIVO' : 'Cada gota = un view. Velocidad = ritmo actual.'}
      </p>
      <canvas
        ref={canvasRef}
        className="w-full rounded-lg"
        style={{ height: '150px', background: '#000a00' }}
      />
    </div>
  )
}
