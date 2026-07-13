import { useEffect, createContext, useContext, useState, useCallback, type ReactNode } from 'react'

interface EasterEggState {
  partyMode: boolean
  matrixMode: boolean
  glitchTick: number
  triggerParty: () => void
  triggerMatrix: () => void
  triggerGlitch: () => void
}

const EasterEggContext = createContext<EasterEggState>({
  partyMode: false,
  matrixMode: false,
  glitchTick: 0,
  triggerParty: () => {},
  triggerMatrix: () => {},
  triggerGlitch: () => {},
})

// Konami code sequence
const KONAMI = ['ArrowUp', 'ArrowUp', 'ArrowDown', 'ArrowDown', 'ArrowLeft', 'ArrowRight', 'ArrowLeft', 'ArrowRight', 'KeyB', 'KeyA']

export function EasterEggProvider({ children }: { children: ReactNode }) {
  const [partyMode, setPartyMode] = useState(false)
  const [matrixMode, setMatrixMode] = useState(false)
  const [glitchTick, setGlitchTick] = useState(0)
  const [keyBuffer, setKeyBuffer] = useState<string[]>([])

  const triggerParty = useCallback(() => {
    setPartyMode(true)
    setTimeout(() => setPartyMode(false), 8000)
  }, [])

  const triggerMatrix = useCallback(() => {
    setMatrixMode(true)
    setTimeout(() => setMatrixMode(false), 5000)
  }, [])

  const triggerGlitch = useCallback(() => {
    setGlitchTick(t => t + 1)
  }, [])

  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      const next = [...keyBuffer, e.code].slice(-10)
      setKeyBuffer(next)
      if (next.join(',') === KONAMI.join(',')) {
        triggerParty()
        setKeyBuffer([])
      }
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [keyBuffer, triggerParty])

  return (
    <EasterEggContext.Provider value={{ partyMode, matrixMode, glitchTick, triggerParty, triggerMatrix, triggerGlitch }}>
      {children}
    </EasterEggContext.Provider>
  )
}

export function useEasterEgg() {
  return useContext(EasterEggContext)
}
