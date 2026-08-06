import { useQuery } from '@tanstack/react-query'
import { api } from '../lib/api'

// ── Dashboard ──
export function useDashboard(channelId?: number) {
  return useQuery({
    queryKey: ['dashboard', channelId ?? 'all'],
    queryFn: () => api.getDashboard(channelId),
    staleTime: 60_000,
    refetchInterval: 120_000, // auto-refresh every 2 minutes
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

export function useLLMCredits() {
  return useQuery({
    queryKey: ['llm-credits'],
    queryFn: () => api.getLLMCredits(),
    refetchInterval: 600_000, // 10 min
    staleTime: 300_000,
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

// ── Active jobs (GenerationContext + GenerationProgressBar) ──
export function useActiveJobs() {
  return useQuery({
    queryKey: ['active-jobs'],
    queryFn: () => api.getActiveJobs(),
    refetchInterval: 15_000,
    staleTime: 10_000,
  })
}

export function useJob(jobId: number) {
  return useQuery({
    queryKey: ['job', jobId],
    queryFn: () => api.getJob(jobId),
    refetchInterval: 3_000,
    staleTime: 1_000,
    enabled: !!jobId,
  })
}

// ── Scheduling sub-components ──
export function usePlannedSlots() {
  return useQuery({
    queryKey: ['planned-slots'],
    queryFn: () => api.getPlannedSlots(),
    refetchInterval: 30_000,
    staleTime: 15_000,
  })
}

export function useShortsPlanningConfig() {
  return useQuery({
    queryKey: ['shorts-planning-config'],
    queryFn: () => api.getShortsPlanningConfig(),
    refetchInterval: 60_000,
    staleTime: 30_000,
  })
}

export function useWeekSlots() {
  return useQuery({
    queryKey: ['week-slots'],
    queryFn: () => api.getWeekSlots(),
    refetchInterval: 60_000,
    staleTime: 30_000,
  })
}

// ── Channel insights ──
export function useLatestInsight(channelId?: number) {
  return useQuery({
    queryKey: ['latest-insight', channelId ?? 0],
    queryFn: () => api.getLatestInsight(channelId!),
    staleTime: 120_000,
    enabled: !!channelId && channelId > 0,
  })
}

// ── Scheduled publish detail ──
export function useVideo(videoId?: number) {
  return useQuery({
    queryKey: ['video', videoId ?? 0],
    queryFn: () => api.getVideo(videoId!),
    refetchInterval: 30_000,
    staleTime: 15_000,
    enabled: !!videoId && videoId > 0,
  })
}
