/** Global context to track active video generation job across pages. */

import { createContext, useContext, useState, useCallback, type ReactNode } from 'react'

export interface ActiveJob {
  jobId: number
  channelId: number
  channelName: string
  action: string
}

interface GenerationContextType {
  activeJob: ActiveJob | null
  setActiveJob: (job: ActiveJob | null) => void
  clearJob: () => void
}

const GenerationContext = createContext<GenerationContextType>({
  activeJob: null,
  setActiveJob: () => {},
  clearJob: () => {},
})

export function GenerationProvider({ children }: { children: ReactNode }) {
  const [activeJob, setActiveJob] = useState<ActiveJob | null>(null)

  const clearJob = useCallback(() => setActiveJob(null), [])

  return (
    <GenerationContext.Provider value={{ activeJob, setActiveJob, clearJob }}>
      {children}
    </GenerationContext.Provider>
  )
}

export function useGeneration() {
  return useContext(GenerationContext)
}
