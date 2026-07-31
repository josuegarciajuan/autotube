import { useQuery } from '@tanstack/react-query'
import { api } from '../lib/api'

// ── Dashboard ──
export function useDashboard(channelId?: number) {
  return useQuery({
    queryKey: ['dashboard', channelId ?? 'all'],
    queryFn: () => api.getDashboard(channelId),
    staleTime: 60_000,
  })
}

// ── Status Bar (always visible, poll every 10s) ──
export function useStatusBar() {
  return useQuery({
    queryKey: ['status-bar'],
    queryFn: () => api.getStatusBar(),
    refetchInterval: 10_000,
    staleTime: 5_000,
  })
}

// ── Monitor page ──
export function useMonitorDashboard() {
  return useQuery({
    queryKey: ['monitor-dashboard'],
    queryFn: () => api.getMonitorDashboard(),
    refetchInterval: 30_000,
    staleTime: 10_000,
  })
}

export function useSystemMetrics() {
  return useQuery({
    queryKey: ['system-metrics'],
    queryFn: () => api.getSystemMetrics(),
    refetchInterval: 5_000,
    staleTime: 2_000,
  })
}

export function useActiveWorkers() {
  return useQuery({
    queryKey: ['active-workers'],
    queryFn: () => api.getActiveWorkers(),
    refetchInterval: 5_000,
    staleTime: 2_000,
  })
}

// ── Pipeline status ──
export function usePipelineStatus() {
  return useQuery({
    queryKey: ['pipeline-status'],
    queryFn: () => api.getPipelineStatus(),
    refetchInterval: 60_000,
    staleTime: 30_000,
  })
}

// ── Scheduling ──
export function useTodaySlots() {
  return useQuery({
    queryKey: ['today-slots'],
    queryFn: () => api.getTodaySlots(),
    refetchInterval: 60_000,
    staleTime: 30_000,
  })
}

export function useShortsSlotsToday() {
  return useQuery({
    queryKey: ['shorts-slots-today'],
    queryFn: () => api.getShortsSlotsToday(),
    refetchInterval: 60_000,
    staleTime: 30_000,
  })
}

export function usePlanningConfig() {
  return useQuery({
    queryKey: ['planning-config'],
    queryFn: () => api.getPlanningConfig(),
    refetchInterval: 60_000,
    staleTime: 30_000,
  })
}

// ── Upcoming publications ──
export function useUpcomingPublications() {
  return useQuery({
    queryKey: ['upcoming-publications'],
    queryFn: () => api.getUpcomingPublications(),
    refetchInterval: 120_000,
    staleTime: 60_000,
  })
}

// ── Channels ──
export function useChannels(activeOnly = false) {
  return useQuery({
    queryKey: ['channels', activeOnly],
    queryFn: () => api.getChannels(activeOnly),
    staleTime: 60_000,
  })
}

export function useChannel(id: number) {
  return useQuery({
    queryKey: ['channel', id],
    queryFn: () => api.getChannel(id),
    staleTime: 60_000,
    enabled: !!id,
  })
}

export function useChannelVideos(id: number, status?: string, playlistId?: number, sourceMode?: string) {
  return useQuery({
    queryKey: ['channel-videos', id, status, playlistId, sourceMode],
    queryFn: () => api.getChannelVideos(id, status, playlistId, sourceMode),
    staleTime: 30_000,
    enabled: !!id,
  })
}

// ── Recent events / console ──
export function useRecentEvents(limit = 20, channelId?: number) {
  return useQuery({
    queryKey: ['recent-events', limit, channelId ?? 'all'],
    queryFn: () => api.getRecentEvents(limit, channelId),
    staleTime: 15_000,
  })
}
