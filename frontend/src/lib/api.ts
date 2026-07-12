/** API client for Autotube backend.
 *  Uses relative URLs so it works whether served at / or /autotube/ */
const API_BASE = 'api';

async function request<T>(url: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${url}`, {
    headers: { 'Content-Type': 'application/json', ...options?.headers },
    ...options,
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail || `HTTP ${res.status}`);
  }
  return res.json();
}

export const api = {
  // Channels
  getChannels: (activeOnly = false) => request<any[]>(`/channels?active_only=${activeOnly}`),
  getChannel: (id: number) => request<any>(`/channels/${id}`),
  createChannel: (data: any) => request<any>('/channels', { method: 'POST', body: JSON.stringify(data) }),
  updateChannel: (id: number, data: any) => request<any>(`/channels/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  updateChannelProfile: (id: number, data: any) => request<any>(`/channels/${id}/profile`, { method: 'PUT', body: JSON.stringify(data) }),
  syncYoutube: (id: number) => request<any>(`/channels/${id}/sync-youtube`, { method: 'POST' }),
  syncChannelConfig: (id: number) => request<any>(`/channels/${id}/sync-config`, { method: 'POST' }),
  deleteChannel: (id: number) => request<any>(`/channels/${id}`, { method: 'DELETE' }),
  getChannelVideos: (id: number, status?: string) => request<any[]>(`/channels/${id}/videos${status ? `?status=${status}` : ''}`),
  getChannelContent: (id: number, unusedOnly = true) => request<any[]>(`/channels/${id}/content?unused_only=${unusedOnly}`),
  getManualSetup: (id: number) => request<any>(`/channels/${id}/manual-setup`),
  getChannelYoutubeStats: (id: number) => request<any>(`/channels/${id}/youtube-stats`),
  getChannelShortsStats: (id: number) => request<any>(`/channels/${id}/shorts-stats`),
  getChannelVideosAggregate: (id: number) => request<any>(`/channels/${id}/videos-aggregate-stats`),
  generateChannelProfile: (id: number) => request<any>(`/channels/${id}/generate-profile`, { method: 'POST' }),
  
  // Templates
  generateTemplate: (channelId: number, segmentType: string) =>
    request<any>(`/channels/${channelId}/templates/${segmentType}/generate`, { method: 'POST' }),
  getTemplates: (channelId: number) =>
    request<any>(`/channels/${channelId}/templates`),
  
  // Auth
  startAuth: (channelId: number) => request<any>(`/channels/${channelId}/auth-start`, { method: 'POST' }),
  submitAuthCode: (channelId: number, code: string) => request<any>(`/channels/${channelId}/auth-code`, { method: 'POST', body: JSON.stringify({ code }) }),
  getAuthStatus: (channelId: number) => request<any>(`/channels/${channelId}/auth-status`),

  // Voices
  getVoices: () => request<any>('/voices'),

  // Videos
  getVideos: (channelId?: number, status?: string, limit = 50) => {
    const params = new URLSearchParams();
    if (channelId) params.set('channel_id', String(channelId));
    if (status) params.set('status', status);
    params.set('limit', String(limit));
    return request<any[]>(`/videos?${params}`);
  },
  getVideo: (id: number) => request<any>(`/videos/${id}`),
  updateVideo: (id: number, data: any) => request<any>(`/videos/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  deleteVideo: (id: number) => request<any>(`/videos/${id}`, { method: 'DELETE' }),
  generateVideo: (data: { channel_id: number; action: string; content_id?: number; test_mode?: boolean; upload?: boolean; source_mode?: string; viral_candidate_id?: number }) =>
    request<any>('/videos/generate', { method: 'POST', body: JSON.stringify(data) }),
  uploadVideo: (id: number) => request<any>(`/videos/${id}/upload`, { method: 'POST' }),
  regenerateThumbnail: (id: number) => request<any>(`/videos/${id}/regenerate-thumbnail`, { method: 'POST' }),

  // Scenes
  getScenes: (videoId: number) => request<any[]>(`/scenes/video/${videoId}`),
  updateScene: (id: number, data: any) => request<any>(`/scenes/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  regenerateSceneAudio: (id: number) => request<any>(`/scenes/${id}/regenerate-audio`, { method: 'POST' }),
  replaceSceneImage: (id: number) => request<any>(`/scenes/${id}/replace-image`, { method: 'POST' }),

  // Content
  getContent: (channelSlug?: string, channelId?: number, unusedOnly = true, status?: string) => {
    const params = new URLSearchParams();
    if (channelSlug) params.set('channel_slug', channelSlug);
    if (channelId) params.set('channel_id', String(channelId));
    params.set('unused_only', String(unusedOnly));
    if (status) params.set('status', status);
    return request<any[]>(`/content?${params}`);
  },
  createContent: (data: any) => request<any>('/content', { method: 'POST', body: JSON.stringify(data) }),
  updateContent: (id: number, data: any) => request<any>(`/content/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  deleteContent: (id: number) => request<any>(`/content/${id}`, { method: 'DELETE' }),
  scheduleContent: (id: number, scheduledAt: string) => 
    request<any>(`/content/${id}/schedule`, { method: 'POST', body: JSON.stringify({ scheduled_at: scheduledAt }) }),
  getScripts: (canal = 'canal2') => request<any[]>(`/content/scripts/list?canal=${canal}`),

  // Jobs
  getJobs: (status?: string, channelId?: number, limit?: number) => {
    const params = new URLSearchParams();
    if (status) params.set('status', status);
    if (channelId) params.set('channel_id', String(channelId));
    if (limit) params.set('limit', String(limit));
    return request<any[]>(`/jobs?${params}`);
  },
  getActiveJobs: () => request<any[]>('/jobs/active'),
  getJob: (id: number) => request<any>(`/jobs/${id}`),

  // Stats
  getStats: () => request<any>('/stats'),
  getDashboard: () => request<any>('/dashboard'),
  getAllChannelStats: () => request<any>('/channels/stats-summary'),
  collectStats: () => request<any>('/stats/collect', { method: 'POST' }),
  getStatsCollectStatus: () => request<any>('/stats/collect/status'),
  getLogs: (channelId?: number) => request<any[]>(`/logs${channelId ? `?channel_id=${channelId}` : ''}`),

  // Marketing AI
  generateMarketingMetadata: (videoId: number) =>
    request<any>(`/videos/${videoId}/generate-metadata`, { method: 'POST' }),

  // Stats
  getVideoStats: (videoIds: string[]) =>
    request<Record<string, any>>('/videos/stats', { method: 'POST', body: JSON.stringify({ video_ids: videoIds }) }),
  getVideoStatsHistory: (videoId: number, days = 30) =>
    request<any[]>(`/videos/${videoId}/stats-history?days=${days}`),

  // Schedules
  getSchedules: (channelId?: number, activeOnly = false) => {
    const params = new URLSearchParams();
    if (channelId) params.set('channel_id', String(channelId));
    if (activeOnly) params.set('active_only', 'true');
    return request<any[]>(`/schedules?${params}`);
  },
  createSchedule: (data: any) => request<any>('/schedules', { method: 'POST', body: JSON.stringify(data) }),
  updateSchedule: (id: number, data: any) => request<any>(`/schedules/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  deleteSchedule: (id: number) => request<any>(`/schedules/${id}`, { method: 'DELETE' }),
  toggleSchedule: (id: number) => request<any>(`/schedules/${id}/toggle`, { method: 'PUT' }),

  // Planning (dynamic scheduling v2)
  getPlanningConfig: () => request<any[]>('/planning/config'),
  updatePlanningConfig: (channelId: number, data: { videos_per_day?: number; planning_enabled?: boolean }) =>
    request<any>(`/planning/config/${channelId}`, { method: 'PUT', body: JSON.stringify(data) }),
  getPlannedSlots: (date?: string, channelId?: number, status?: string) => {
    const params = new URLSearchParams();
    if (date) params.set('date', date);
    if (channelId) params.set('channel_id', String(channelId));
    if (status) params.set('status', status);
    return request<any[]>(`/planning/slots?${params}`);
  },
  getTodaySlots: () => request<any>('/planning/slots/today'),
  getWeekSlots: (channelId?: number) => {
    const params = new URLSearchParams();
    if (channelId) params.set('channel_id', String(channelId));
    return request<any>(`/planning/slots/week?${params}`);
  },
  updateSlotSourceMode: (slotId: number, sourceMode: string) =>
    request<any>(`/planning/slots/${slotId}/mode`, { method: 'PUT', body: JSON.stringify({ source_mode: sourceMode }) }),
  getPlanningTimeline: () => request<any>('/planning/timeline'),
  getPlanningStats: () => request<any>('/planning/stats'),
  forceReplan: (date?: string) => {
    const params = date ? `?date=${date}` : '';
    return request<any>(`/planning/replan${params}`, { method: 'POST' });
  },
  previewWeek: (overrides?: Record<string, any>) =>
    request<any>('/planning/preview', {
      method: 'POST',
      body: JSON.stringify({ overrides: overrides || {} }),
    }),

  // Shorts Planning
  getShortsPlanningConfig: () => request<any[]>('/planning/shorts-config'),
  updateShortsPlanningConfig: (channelId: number, data: { shorts_enabled?: boolean; shorts_native_per_day?: number; shorts_clip_per_day?: number }) =>
    request<any>(`/planning/shorts-config/${channelId}`, { method: 'PUT', body: JSON.stringify(data) }),
  replanShorts: () => request<any>('/planning/shorts-replan', { method: 'POST' }),

  // Shorts slots
  getShortsSlotsToday: () => request<any>('/planning/shorts-slots/today'),
  getShortsSlotsWeek: (channelId?: number) => {
    const params = channelId ? `?channel_id=${channelId}` : ''
    return request<any>(`/planning/shorts-slots/week${params}`)
  },

  // System
  stabilizeSystem: () => request<any>('/system/stabilize', { method: 'POST' }),

  // Monetization
  getChannelMonetization: (channelId: number) =>
    request<any>(`/channels/${channelId}/monetization`),
  getMonetizationOverview: () =>
    request<any>('/monetization/overview'),
  updateChannelMonetization: (channelId: number, data: any) =>
    request<any>(`/channels/${channelId}/monetization`, { method: 'PUT', body: JSON.stringify(data) }),

  // Milestones
  getChannelMilestones: (channelId: number) =>
    request<any>(`/channels/${channelId}/milestones`),
  getMilestonesOverview: (limit = 8) =>
    request<any>(`/milestones/overview?limit=${limit}`),

  // Analytics
  getChannelGrowth: (channelId: number, days = 30) =>
    request<any>(`/channels/${channelId}/analytics/growth?days=${days}`),
  getChannelContentRanking: (channelId: number, sort = 'views', limit = 20) =>
    request<any>(`/channels/${channelId}/analytics/content?sort=${sort}&limit=${limit}`),
  getVideoAnalytics: (videoId: number) =>
    request<any>(`/videos/${videoId}/analytics`),
  getChannelsComparison: () =>
    request<any>('/analytics/comparison'),

  // ── Promotion / Lifecycle ──
  // Playlists
  getChannelPlaylists: (channelId: number) =>
    request<any[]>(`/channels/${channelId}/playlists`),
  syncChannelPlaylists: (channelId: number) =>
    request<any>(`/channels/${channelId}/playlists/sync`, { method: 'POST' }),
  getVideoPlaylists: (videoId: number) =>
    request<any[]>(`/videos/${videoId}/playlists`),
  addVideoToPlaylists: (videoId: number) =>
    request<any>(`/videos/${videoId}/add-to-playlists`, { method: 'POST' }),

  // Lifecycle
  getVideoLifecycle: (videoId: number) =>
    request<any[]>(`/videos/${videoId}/lifecycle`),
  getChannelLifecycleRecent: (channelId: number, limit = 30) =>
    request<any[]>(`/channels/${channelId}/lifecycle/recent?limit=${limit}`),
  triggerLifecycleAction: (videoId: number, actionType: string) =>
    request<any>(`/videos/${videoId}/lifecycle/trigger`, {
      method: 'POST',
      body: JSON.stringify({ action_type: actionType }),
    }),

  // Comments
  postFirstComment: (videoId: number) =>
    request<any>(`/videos/${videoId}/post-first-comment`, { method: 'POST' }),
  replyToComments: (videoId: number) =>
    request<any>(`/videos/${videoId}/reply-comments`, { method: 'POST' }),

  // Metadata
  reoptimizeMetadata: (videoId: number) =>
    request<any>(`/videos/${videoId}/reoptimize-metadata`, { method: 'POST' }),

  // ── Scheduled publishing ──────────────────────────────
  getScheduledMode: (channelId: number) =>
    request<{ channel_id: number; publish_mode: string; channel_name: string }>(`/channels/${channelId}/scheduled-mode`),

  toggleScheduledMode: (channelId: number) =>
    request<{ ok: boolean; channel_id: number; publish_mode: string; previous_mode: string }>(`/channels/${channelId}/scheduled-mode/toggle`, { method: 'POST' }),

  getUpcomingPublications: (channelId?: number, days?: number) => {
    const params = new URLSearchParams();
    if (channelId) params.set('channel_id', String(channelId));
    if (days) params.set('days', String(days));
    return request<any[]>(`/videos/publications/upcoming?${params}`);
  },

  getPendingManualActions: (channelId?: number) => {
    const params = new URLSearchParams();
    if (channelId) params.set('channel_id', String(channelId));
    return request<any>(`/videos/manual-actions/pending?${params}`);
  },

  updateManualChecklist: (videoId: number, item: string, done: boolean) =>
    request<any>(`/videos/${videoId}/manual-checklist`, {
      method: 'PATCH',
      body: JSON.stringify({ item, done }),
    }),

  publishNow: (videoId: number) =>
    request<any>(`/videos/${videoId}/publish-now`, { method: 'POST' }),

  cancelSchedule: (videoId: number) =>
    request<any>(`/videos/${videoId}/cancel-schedule`, { method: 'POST' }),

  // Peak info per channel
  getChannelPeakInfo: (channelId: number) =>
    request<{
      channel_id: number; channel_name: string; publish_mode: string;
      peak_hour: number; secondary_peaks: number[];
      jitter_min: number; timezone: string; warmup_min: number;
      source: string; niche: string;
    }>(`/channels/${channelId}/peak-info`),
};

/** Format seconds to mm:ss */
export function formatDuration(seconds: number): string {
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${m}:${s.toString().padStart(2, '0')}`;
}

/** Format date string — DB stores server local time (Europe/Madrid) */
export function formatDate(dateStr: string | null): string {
  if (!dateStr) return '-';
  const d = new Date(dateStr.replace(' ', 'T'));
  return d.toLocaleDateString('es-ES', { day: '2-digit', month: '2-digit', year: 'numeric' });
}

/** Format date + time — DB stores server local time (Europe/Madrid) */
export function formatDateTime(dateStr: string | null): string {
  if (!dateStr) return '-';
  const d = new Date(dateStr.replace(' ', 'T'));
  return d.toLocaleDateString('es-ES', { day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit' });
}

/** Format milliseconds to human-readable minutes */
export function formatTimingMs(ms: number | null | undefined): string {
  if (!ms) return '';
  const minutes = Math.floor(ms / 60000);
  if (minutes < 1) return '<1min';
  return `${minutes}min`;
}

/** File size formatter */
export function formatFileSize(bytes: number): string {
  if (!bytes) return '0 MB';
  const mb = bytes / (1024 * 1024);
  return `${mb.toFixed(1)} MB`;
}

/** Truncate text */
export function truncate(text: string, max: number): string {
  if (!text) return '';
  return text.length > max ? text.slice(0, max) + '...' : text;
}

/** Format big numbers: 1000 → 1K, 1500000 → 1.5M */
export function formatShortNumber(num: string | number): string {
  const n = typeof num === 'string' ? parseInt(num, 10) : num;
  if (isNaN(n)) return '0';
  if (n >= 1000000) return (n / 1000000).toFixed(1).replace('.0', '') + 'M';
  if (n >= 1000) return (n / 1000).toFixed(1).replace('.0', '') + 'K';
  return n.toString();
}

/** Get status badge class */
export function statusBadge(status: string): string {
  const map: Record<string, string> = {
    draft: 'badge-draft',
    generating: 'badge-generating',
    reassembling: 'badge-reassembling',
    ready: 'badge-ready',
    uploaded: 'badge-uploaded',
    error: 'badge-error',
    uploaded_private: 'badge-uploaded_private',
    warming: 'badge-warming',
    scheduled: 'badge-scheduled',
    published: 'badge-published',
  };
  return map[status] || 'badge-draft';
}

/** Get status label in Spanish */
export function statusLabel(status: string): string {
  const map: Record<string, string> = {
    draft: 'Borrador',
    generating: 'Generando',
    reassembling: 'Re-ensamblando',
    ready: 'Listo',
    uploaded: 'Subido',
    error: 'Error',
    uploaded_private: 'Subido (privado)',
    warming: 'Calentando',
    scheduled: 'Programado',
    published: 'Publicado',
  };
  return map[status] || status;
}

/** Convert a stored media path (absolute or relative) to a client-side URL.
 *
 *  The backend resolves DB paths via resolve_media_path() which handles:
 *    - /root/autotube/output/...  (absolute under project root)
 *    - output/...                 (project-root-relative)
 *    - /tmp/...                   (external absolute)
 *
 *  Returns a relative URL that respects the <base href="/autotube/">.
 *  Prefer apiUrl() for dedicated endpoints — this is for generic files. */
export function mediaUrl(storedPath: string | null | undefined): string {
  if (!storedPath) return '';
  const clean = storedPath.startsWith('/root/autotube/')
    ? storedPath.slice('/root/autotube/'.length)
    : storedPath.startsWith('/')
      ? storedPath.slice(1)
      : storedPath;
  return `api/static/${clean}`;
}

/** Build a relative API URL that respects the <base href="/autotube/"> */
export function apiUrl(path: string): string {
  return `api${path.startsWith('/') ? path : `/${path}`}`;
}

/** Format a target time into a countdown string in Spanish */
export function formatCountdown(targetIso: string | null): string {
  if (!targetIso) return '';
  try {
    const target = new Date(targetIso.replace(' ', 'T'));
    const now = new Date();
    const diff = target.getTime() - now.getTime();
    if (diff <= 0) return 'Ahora';
    const mins = Math.floor(diff / 60000);
    if (mins < 60) return `${mins}m`;
    const hours = Math.floor(mins / 60);
    if (hours < 24) return `${hours}h ${mins % 60}m`;
    const days = Math.floor(hours / 24);
    return `${days}d ${hours % 24}h`;
  } catch {
    return '';
  }
}

/** Format target time as HH:MM (local) with jitter indicator */
export function formatTargetTime(targetIso: string | null): string {
  if (!targetIso) return '';
  try {
    const d = new Date(targetIso.replace(' ', 'T'));
    return d.toLocaleTimeString('es-ES', { hour: '2-digit', minute: '2-digit' });
  } catch {
    return '';
  }
}

// ═══════════════════════════════════════════════════════════════
// TypeScript interfaces for scheduled publishing
// ═══════════════════════════════════════════════════════════════

export interface UpcomingPublication {
  video_id: number;
  channel_id: number;
  channel_name: string;
  channel_slug: string;
  titulo_final: string;
  status: string;
  target_public_at: string | null;
  peak_source: string;
  published_at: string | null;
  auto_playlist_name: string | null;
  remaining_seconds: number;
  pending_altered: number;
  pending_endscreens: number;
  yt_video_id: string | null;
  yt_url: string | null;
  uploaded_at: string | null;
}

export interface PendingManualSummary {
  total_pending: number;
  total_affected_videos: number;
  channels: Record<string, {
    channel_id: number;
    channel_name: string;
    channel_slug: string;
    pending_altered: number;
    pending_endscreens: number;
    total_pending: number;
    affected_videos: number;
  }>;
}
