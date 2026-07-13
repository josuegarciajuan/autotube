import { createContext, useContext, useState, useCallback, type ReactNode } from 'react'

interface ChannelFilterState {
  selectedChannelId: number | null  // null = all channels
  setSelectedChannelId: (id: number | null) => void
  channels: Array<{ id: number; name: string; slug: string }>
  setChannels: (chs: Array<{ id: number; name: string; slug: string }>) => void
}

const ChannelFilterContext = createContext<ChannelFilterState>({
  selectedChannelId: null,
  setSelectedChannelId: () => {},
  channels: [],
  setChannels: () => {},
})

export function ChannelFilterProvider({ children }: { children: ReactNode }) {
  const [selectedChannelId, setSelectedChannelId] = useState<number | null>(null)
  const [channels, setChannels] = useState<Array<{ id: number; name: string; slug: string }>>([])

  return (
    <ChannelFilterContext.Provider value={{ selectedChannelId, setSelectedChannelId, channels, setChannels }}>
      {children}
    </ChannelFilterContext.Provider>
  )
}

export function useChannelFilter() {
  return useContext(ChannelFilterContext)
}
