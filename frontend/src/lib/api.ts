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
  generateChannelProfile: (id: number) => request<any>(`/channels/${id}/generate-profile`, { method: 'POST' }),
  
  // Auth
  startAuth: (channelId: number) => request<any>(`/channels/${channelId}/auth-start`, { method: 'POST' }),
  submitAuthCode: (channelId: number, code: string) => request<any>(`/channels/${channelId}/auth-code`, { method: 'POST', body: JSON.stringify({ code }) }),
  getAuthStatus: (channelId: number) => request<any>(`/channels/${channelId}/auth-status`),

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
  generateVideo: (data: { channel_id: number; action: string; content_id?: number }) =>
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
  getScripts: (canal = 'canal1') => request<any[]>(`/content/scripts/list?canal=${canal}`),

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
};

/** Format seconds to mm:ss */
export function formatDuration(seconds: number): string {
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${m}:${s.toString().padStart(2, '0')}`;
}

/** Format date string */
export function formatDate(dateStr: string | null): string {
  if (!dateStr) return '-';
  const d = new Date(dateStr);
  return d.toLocaleDateString('es-ES', { day: '2-digit', month: '2-digit', year: 'numeric' });
}

/** Format date + time */
export function formatDateTime(dateStr: string | null): string {
  if (!dateStr) return '-';
  const d = new Date(dateStr);
  return d.toLocaleDateString('es-ES', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });
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
    ready: 'badge-ready',
    uploaded: 'badge-uploaded',
    error: 'badge-error',
  };
  return map[status] || 'badge-draft';
}

/** Get status label in Spanish */
export function statusLabel(status: string): string {
  const map: Record<string, string> = {
    draft: 'Borrador',
    generating: 'Generando',
    ready: 'Listo',
    uploaded: 'Subido',
    error: 'Error',
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
