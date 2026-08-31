/** API client for Autotube backend.
 *  Uses absolute /api prefix — works with Vite proxy (dev) and direct (prod). */
const API_BASE = '/api';

export interface FullReplanPreflight {
  confirmation_token: string;
  expires_at: string;
  proposed_slots: FullReplanProposedSlot[];
  summary: {
    proposed: number;
    horizon_days: number;
  };
}

export interface FullReplanProposedSlot {
  channel_id: number;
  date_key: string;
  scheduled_at: string;
}

export interface FullReplanApplyResult {
  ok: boolean;
  updated: number;
  created: number;
  preserved: number;
}

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
  // Pacing (strike mode profile)
  getPacingProfile: () => request<any>(`/pacing/profile`),
  setPacingProfile: (profile: string) => request<any>(`/pacing/profile`, { method: 'PUT', body: JSON.stringify({ profile }) }),
  getFactoryStatus: () => request<any>(`/pacing/factory-status`),
  // Channels
  getChannels: (activeOnly = false) => request<any[]>(`/channels?active_only=${activeOnly}`),
  getChannel: (id: number) => request<any>(`/channels/${id}`),
  createChannel: (data: any) => request<any>('/channels', { method: 'POST', body: JSON.stringify(data) }),
  updateChannel: (id: number, data: any) => request<any>(`/channels/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  updateChannelProfile: (id: number, data: any) => request<any>(`/channels/${id}/profile`, { method: 'PUT', body: JSON.stringify(data) }),
  syncYoutube: (id: number) => request<any>(`/channels/${id}/sync-youtube`, { method: 'POST' }),
  collectChannelStats: (id: number) => request<any>(`/channels/${id}/collect-stats`, { method: 'POST' }),
  syncChannelConfig: (id: number) => request<any>(`/channels/${id}/sync-config`, { method: 'POST' }),
  checkChannelEgress: (id: number) => request<any>(`/channels/${id}/check-egress`, { method: 'POST' }),
  deleteChannel: (id: number) => request<any>(`/channels/${id}`, { method: 'DELETE' }),
  getChannelVideos: (id: number, status?: string, playlistId?: number, sourceMode?: string, limit?: number, offset?: number) => {
    const params = new URLSearchParams();
    if (status) params.set('status', status);
    if (playlistId) params.set('playlist_id', String(playlistId));
    if (sourceMode) params.set('source_mode', sourceMode);
    if (limit !== undefined) params.set('limit', String(limit));
    if (offset !== undefined) params.set('offset', String(offset));
    const qs = params.toString();
    return request<any[]>(`/channels/${id}/videos${qs ? `?${qs}` : ''}`);
  },
  getChannelContent: (id: number, unusedOnly = true) => request<any[]>(`/channels/${id}/content?unused_only=${unusedOnly}`),
  cleanupErrorVideos: (id: number, olderThanDays: number = 7, dryRun: boolean = false) => {
    const params = new URLSearchParams({ older_than_days: String(olderThanDays), dry_run: String(dryRun) });
    return request<any>(`/channels/${id}/videos/cleanup-errors?${params}`, { method: 'DELETE' });
  },
  getManualSetup: (id: number) => request<any>(`/channels/${id}/manual-setup`),
  getChannelYoutubeStats: (id: number) => request<any>(`/channels/${id}/youtube-stats`),
  getChannelShortsStats: (id: number) => request<any>(`/channels/${id}/shorts-stats`),
  getShortTypeComparison: (id: number, days: number = 30) =>
    request<any>(`/channels/${id}/analytics/short-types?days=${days}`),
  getChannelVideosAggregate: (id: number) => request<any>(`/channels/${id}/videos-aggregate-stats`),
  getChannelMarathonAnalytics: (id: number) => request<any>(`/channels/${id}/analytics/marathons`),
  generateChannelProfile: (id: number) => request<any>(`/channels/${id}/generate-profile`, { method: 'POST' }),
  
  // Social Media Accounts
  getSocialAccounts: (channelId: number) => request<any[]>(`/channels/${channelId}/social-accounts`),
  saveSocialAccount: (channelId: number, platform: string, data: { username: string; password: string; enabled: boolean; account_email?: string; account_email_password?: string; account_password?: string; notes?: string }) =>
    request<any>(`/channels/${channelId}/social-accounts/${platform}`, { method: 'PUT', body: JSON.stringify(data) }),
  updateSocialAccount: (channelId: number, platform: string, data: { username?: string; password?: string; enabled?: boolean; account_email?: string; account_email_password?: string; account_password?: string; notes?: string }) =>
    request<any>(`/channels/${channelId}/social-accounts/${platform}`, { method: 'PATCH', body: JSON.stringify(data) }),
  revealSocialCredential: (channelId: number, platform: string, field: string) =>
    request<any>(`/channels/${channelId}/social-accounts/${platform}/reveal`, { method: 'POST', body: JSON.stringify({ field }) }),
  deleteSocialAccount: (channelId: number, platform: string) =>
    request<any>(`/channels/${channelId}/social-accounts/${platform}`, { method: 'DELETE' }),
  testSocialLogin: (channelId: number, platform: string) =>
    request<any>(`/channels/${channelId}/social-accounts/${platform}/test`, { method: 'POST' }),
  getSocialTiming: (channelId: number) => request<any>(`/channels/${channelId}/social-timing`),
  updateSocialTiming: (channelId: number, data: Record<string, number>) =>
    request<any>(`/channels/${channelId}/social-timing`, { method: 'PUT', body: JSON.stringify(data) }),
  
  // Cross-Platform Publishing (v27)
  getCrossPlatformConfig: (channelId: number) =>
    request<any>(`/channels/${channelId}/cross-platform-config`),
  updateCrossPlatformConfig: (channelId: number, data: { facebook?: boolean; rumble?: boolean; tiktok?: boolean }) =>
    request<any>(`/channels/${channelId}/cross-platform-config`, { method: 'PUT', body: JSON.stringify(data) }),
  getVideoPlatformStatus: (videoId: number) =>
    request<any[]>(`/videos/${videoId}/platform-status`),
  publishToPlatform: (videoId: number, platform: string) =>
    request<any>(`/videos/${videoId}/publish-to/${platform}`, { method: 'POST' }),
  republishToPlatform: (videoId: number, platform: string) =>
    request<any>(`/videos/${videoId}/republish-to/${platform}`, { method: 'POST' }),
  getChannelPlatformStats: (channelId: number) =>
    request<any[]>(`/channels/${channelId}/platform-stats`),

  // Social redistribution (v44)
  getRedistributionStatus: (channelId: number) =>
    request<any>(`/channels/${channelId}/redistribution/status`),
  startRedistribution: (channelId: number) =>
    request<any>(`/channels/${channelId}/redistribution/start`, { method: 'POST' }),
  pauseRedistribution: (channelId: number) =>
    request<any>(`/channels/${channelId}/redistribution/pause`, { method: 'POST' }),
  resumeRedistribution: (channelId: number) =>
    request<any>(`/channels/${channelId}/redistribution/resume`, { method: 'POST' }),
  enqueueRedistribution: (channelId: number) =>
    request<any>(`/channels/${channelId}/redistribution/enqueue`, { method: 'POST' }),
  getRedistributionBacklog: (channelId: number, platform?: string, limit = 50) => {
    const params = new URLSearchParams({ limit: String(limit) })
    if (platform) params.set('platform', platform)
    return request<any[]>(`/channels/${channelId}/redistribution/backlog?${params}`)
  },
  getChannelSocialStats: (channelId: number) =>
    request<any>(`/channels/${channelId}/social-stats`),
  getChannelVideosSocialStats: (channelId: number) =>
    request<any>(`/channels/${channelId}/videos-social-stats`),
  getVideoSocialStats: (videoId: number) =>
    request<any>(`/videos/${videoId}/social-stats`),
  collectSocialStats: (channelId?: number) => {
    const qs = channelId ? `?channel_id=${channelId}` : ''
    return request<any>(`/social-stats/collect${qs}`, { method: 'POST' })
  },
  
  // Templates
  generateTemplate: (channelId: number, segmentType: string) =>
    request<any>(`/channels/${channelId}/templates/${segmentType}/generate`, { method: 'POST' }),
  getTemplates: (channelId: number) =>
    request<any>(`/channels/${channelId}/templates`),
  
  // Auth
  startAuth: (channelId: number) => request<any>(`/channels/${channelId}/auth-start`, { method: 'POST' }),
  submitAuthCode: (channelId: number, code: string) => request<any>(`/channels/${channelId}/auth-code`, { method: 'POST', body: JSON.stringify({ code }) }),
  getAuthStatus: (channelId: number) => request<any>(`/channels/${channelId}/auth-status`),
  getBrowserSessionStatus: () => request<{accounts: any[]; all_valid: boolean; any_invalid: boolean}>('/browser-sessions/status'),

  // Voices
  getVoices: () => request<any>('/voices'),

  // Videos
  getVideos: (channelId?: number, status?: string, limit = 50, playlistId?: number) => {
    const params = new URLSearchParams();
    if (channelId) params.set('channel_id', String(channelId));
    if (status) params.set('status', status);
    params.set('limit', String(limit));
    if (playlistId) params.set('playlist_id', String(playlistId));
    return request<any[]>(`/videos?${params}`);
  },
  getVideo: (id: number) => request<any>(`/videos/${id}`),
  updateVideo: (id: number, data: any) => request<any>(`/videos/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  deleteVideo: (id: number) => request<any>(`/videos/${id}`, { method: 'DELETE' }),
  generateVideo: (data: { channel_id: number; action: string; content_id?: number; test_mode?: boolean; upload?: boolean; source_mode?: string; viral_candidate_id?: number }) =>
    request<any>('/videos/generate', { method: 'POST', body: JSON.stringify(data) }),
  uploadVideo: (id: number) => request<any>(`/videos/${id}/upload`, { method: 'POST' }),
  regenerateThumbnail: (id: number) => request<any>(`/videos/${id}/regenerate-thumbnail`, { method: 'POST' }),
  retryThumbnail: (id: number) => request<any>(`/videos/${id}/retry-thumbnail-upload`, { method: 'POST' }),

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
  getScripts: (canal: string) => request<any[]>(`/content/scripts/list?canal=${canal}`),

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
  cancelJob: (id: number) => request<any>(`/jobs/${id}/cancel`, { method: 'POST' }),

  // Stats
  getStats: () => request<any>('/stats'),
  getDashboard: (channelId?: number) => {
    const qs = channelId != null ? `?channel_id=${channelId}` : ''
    return request<any>(`/dashboard${qs}`)
  },
  getAllChannelStats: () => request<any>('/channels/stats-summary'),
  collectStats: (deep?: boolean) =>
    request<any>(`/stats/collect${deep ? '?deep=true' : ''}`, { method: 'POST' }),
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

  // A/B Testing (v31 — sequential title/thumbnail optimization)
  getVideoABTestStatus: (videoId: number) =>
    request<any>(`/videos/${videoId}/ab-test/status`),
  triggerABTest: (videoId: number, phase?: string) => {
    const qs = phase ? `?phase=${encodeURIComponent(phase)}` : ''
    return request<any>(`/videos/${videoId}/ab-test/trigger${qs}`, { method: 'POST' })
  },

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
  updatePlanningConfig: (channelId: number, data: { videos_per_day?: number; planning_enabled?: boolean; viral_per_day?: number; videos_day_boost_weight?: number; viral_day_boost_weight?: number }) =>
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

  // Full Replan — preview first, then apply the short-lived reviewed plan.
  fullReplanPreflight: () => request<FullReplanPreflight>('/planning/full-replan/preflight', { method: 'POST' }),
  fullReplanApply: (confirmationToken: string) => request<FullReplanApplyResult>('/planning/full-replan/apply', {
    method: 'POST',
    body: JSON.stringify({ confirmation_token: confirmationToken }),
  }),

  // Optimal Publish Slots (v10)
  getOptimalSlots: (channelId: number) =>
    request<any>(`/channels/${channelId}/optimal-slots`),
  recalculateOptimalSlotsAll: () =>
    request<any>('/planning/recalculate-optimal-slots', { method: 'POST' }),

  // Timing Dashboard (v11) — Horarios tab
  getTimingDashboard: (channelId: number, days?: number) => {
    const params = days ? `?days=${days}` : ''
    return request<TimingDashboardResponse>(`/channels/${channelId}/timing-dashboard${params}`)
  },

  // Shorts slots
  getShortsSlotsToday: () => request<any>('/planning/shorts-slots/today'),
  getShortsSlotsWeek: (channelId?: number) => {
    const params = channelId ? `?channel_id=${channelId}` : ''
    return request<any>(`/planning/shorts-slots/week${params}`)
  },
  // Sube ahora un short nativo en cola (status='generated') — fix ago 2026
  uploadQueuedShort: (shortId: number) =>
    request<any>(`/planning/shorts-queue/upload/${shortId}`, { method: 'POST' }),

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
  getThumbnailStyleCtr: (channelId: number) =>
    request<any>(`/channels/${channelId}/analytics/thumbnail-styles`),
  getVideoAnalytics: (videoId: number) =>
    request<any>(`/videos/${videoId}/analytics`),
  getChannelsComparison: () =>
    request<any>('/analytics/comparison'),
  getChannelWatchTime: (channelId: number) =>
    request<any>(`/channels/${channelId}/analytics/watch-time`),
  // Advanced analytics (CTR, traffic, demographics)
  getChannelCTR: (channelId: number) =>
    request<any>(`/channels/${channelId}/analytics/ctr`),
  getChannelTraffic: (channelId: number) =>
    request<any>(`/channels/${channelId}/analytics/traffic`),
  getChannelDemographics: (channelId: number) =>
    request<any>(`/channels/${channelId}/analytics/demographics`),

  // ── SEO ──
  getChannelSEOScore: (channelId: number) =>
    request<any>(`/channels/${channelId}/seo-score`),
  keywordResearch: (topic: string, channelId: number, geo = 'ES') =>
    request<any>(`/seo/keyword-research?topic=${encodeURIComponent(topic)}&channel_id=${channelId}&geo=${geo}`, { method: 'POST' }),
  getChannelsSEO: () =>
    request<any>('/analytics/comparison'),  // includes CTR data per channel

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

  // Reprogramar publicaciones pendientes del canal con gaps >=3h
  reprogramChannelPublish: (channelId: number, dryRun: boolean = false) =>
    request<any>(`/channels/${channelId}/reprogram-publish?dry_run=${dryRun}`, { method: 'POST' }),

  getUpcomingPublications: (channelId?: number, days?: number) => {
    const params = new URLSearchParams();
    if (channelId) params.set('channel_id', String(channelId));
    if (days) params.set('days', String(days));
    return request<any[]>(`/videos/publications/upcoming?${params}`);
  },

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

  // Pipeline status (visual scheduling view)
  getPipelineStatus: () =>
    request<{
      planned: PlannedSlot[];
      generating: GeneratingVideo[];
      awaiting_upload: AwaitingUploadVideo[];
      warming: WarmingVideo[];
      published_24h: PublishedItem[];
      shorts: { pending: ShortsPipelineSlot[]; generating: ShortsPipelineSlot[]; completed: ShortsPipelineSlot[]; ready_to_upload: ShortsPipelineSlot[] };
    }>('/planning/pipeline-status'),

  // ── Gamification v3 ─────────────────────────────────
  getStreaks: (channelId?: number) => {
    const params = channelId ? `?channel_id=${channelId}` : ''
    return request<any[]>(`/streaks${params}`)
  },

  getBadges: (channelId?: number) => {
    const params = channelId ? `?channel_id=${channelId}` : ''
    return request<any[]>(`/badges${params}`)
  },

  checkBadges: (channelId?: number) => {
    const params = channelId ? `?channel_id=${channelId}` : ''
    return request<any>(`/badges/check${params}`, { method: 'POST' })
  },

  getRecentEvents: (limit = 50, channelId?: number) => {
    let url = `/events/recent?limit=${limit}`
    if (channelId) url += `&channel_id=${channelId}`
    return request<any[]>(url)
  },

  getContentFlow: (channelId: number) =>
    request<any>(`/channels/${channelId}/content-flow`),

  // Monitor (lifecycle events + alerts)
  getMonitorDashboard: () => request<any>('/monitor/dashboard'),
  getMonitorAlerts: (status?: string, severity?: string, entityType?: string, channelId?: number, limit?: number) => {
    const params = new URLSearchParams()
    if (status) params.set('status', status)
    if (severity) params.set('severity', severity)
    if (entityType) params.set('entity_type', entityType)
    if (channelId) params.set('channel_id', String(channelId))
    if (limit) params.set('limit', String(limit))
    const qs = params.toString()
    return request<{alerts: any[]}>(`/monitor/alerts${qs ? `?${qs}` : ''}`)
  },
  getMonitorEvents: (entityType?: string, entityId?: number, channelId?: number, limit?: number) => {
    const params = new URLSearchParams()
    if (entityType) params.set('entity_type', entityType)
    if (entityId) params.set('entity_id', String(entityId))
    if (channelId) params.set('channel_id', String(channelId))
    if (limit) params.set('limit', String(limit || 100))
    return request<{events: any[]}>(`/monitor/events?${params.toString()}`)
  },
  acknowledgeMonitorAlert: (alertId: number) =>
    request<any>(`/monitor/alerts/${alertId}/acknowledge`, { method: 'POST' }),
  resolveMonitorAlert: (alertId: number) =>
    request<any>(`/monitor/alerts/${alertId}/resolve`, { method: 'POST' }),
  resolveAllMonitorAlerts: (severity?: string) => {
    const params = severity ? `?severity=${severity}` : ''
    return request<any>(`/monitor/alerts/resolve-all${params}`, { method: 'POST' })
  },
  getSilencedAlertTypes: () => request<any>('/monitor/alerts/silenced-types'),
  silenceAlertType: (alertType: string, silenced: boolean = true) =>
    request<any>(`/monitor/alerts/silence-type?alert_type=${encodeURIComponent(alertType)}&silenced=${silenced}`, { method: 'POST' }),
  triggerHealthCheck: () =>
    request<any>('/monitor/health-check', { method: 'POST' }),
  getSystemMetrics: () => request<any>('/monitor/system'),
  getActiveWorkers: () => request<any>('/monitor/workers'),
  getStatusBar: () => request<any>('/monitor/status-bar'),
  getQuotaStatus: () => request<any>('/system/quota-status'),
  getSpamBlocks: () => request<any>('/system/spam-blocks'),
  unblockSpamChannel: (channelId: number) =>
    request<any>(`/system/spam-blocks/${channelId}/unblock`, { method: 'POST' }),
  restoreSpamFrequency: (channelId: number) =>
    request<any>(`/system/spam-blocks/${channelId}/restore-frequency`, { method: 'POST' }),
  getSpamReport: (channelId: number) =>
    request<any>(`/system/spam-blocks/${channelId}/report`),
  getResumeStatus: (channelId?: number) => {
    const qs = channelId ? `?channel_id=${channelId}` : ''
    return request<any>(`/system/resume-status${qs}`)
  },
  getChannelRestrictions: () => request<any>('/system/channel-restrictions'),
  studioScan: (channelId: number) =>
    request<any>(`/system/studio-scan?channel_id=${channelId}`, { method: 'POST' }),
  applyResumePhases: () =>
    request<any>('/system/resume/apply', { method: 'POST' }),
  getLLMCredits: () => request<any>('/monitor/llm-credits'),
  triggerLLMCreditCheck: () => request<any>('/monitor/llm-credits/check', { method: 'POST' }),

  // ── View Gap Monitor ──
  getViewGapCoverage: (channelId?: number) => {
    const qs = channelId ? `?channel_id=${channelId}` : ''
    return request<any>(`/monitor/view-gap/coverage${qs}`)
  },
  getUnregisteredVideos: (channelId?: number, limit = 50) => {
    const params = new URLSearchParams()
    if (channelId) params.set('channel_id', String(channelId))
    params.set('limit', String(limit))
    return request<any>(`/monitor/view-gap/unregistered?${params}`)
  },
  triggerViewGapScan: (channelId: number) =>
    request<any>(`/monitor/view-gap/scan/${channelId}`, { method: 'POST' }),
  triggerViewGapScanAll: () =>
    request<any>('/monitor/view-gap/scan-all', { method: 'POST' }),
  getChannelViewGap: (channelId: number) =>
    request<any>(`/channels/${channelId}/view-gap`),

  // ── Insights AI (v20 — AI self-optimization) ──
  analyzeChannel: (channelId: number) =>
    request<any>(`/channels/${channelId}/analyze`, { method: 'POST' }),
  cancelAnalysis: (channelId: number) =>
    request<any>(`/channels/${channelId}/analyze`, { method: 'DELETE' }),
  getLatestInsight: (channelId: number, signal?: AbortSignal) =>
    request<any>(`/channels/${channelId}/insights/latest`, signal ? { signal } : undefined),
  applyInsight: (channelId: number, insightId: number, recId: string, refinedVersionIndex?: number) => {
    let url = `/channels/${channelId}/insights/${insightId}/apply?rec_id=${encodeURIComponent(recId)}`
    if (refinedVersionIndex !== undefined && refinedVersionIndex >= 0) {
      url += `&refined_version_index=${refinedVersionIndex}`
    }
    return request<any>(url, { method: 'POST' })
  },
  validateInsight: (channelId: number, insightId: number, recId: string) =>
    request<any>(
      `/channels/${channelId}/insights/${insightId}/validate?rec_id=${encodeURIComponent(recId)}`,
      { method: 'POST' }
    ),
  refineInsight: (
    channelId: number,
    insightId: number,
    recId: string,
    userFeedback: string,
    conversationHistory?: { role: string; content: string }[]
  ) =>
    request<any>(
      `/channels/${channelId}/insights/${insightId}/refine`,
      {
        method: 'POST',
        body: JSON.stringify({
          rec_id: recId,
          user_feedback: userFeedback,
          conversation_history: conversationHistory,
        }),
      }
    ),
  discardInsight: (channelId: number, insightId: number, recId: string, discarded: boolean) =>
    request<any>(
      `/channels/${channelId}/insights/${insightId}/discard?rec_id=${encodeURIComponent(recId)}&discarded=${discarded}`,
      { method: 'POST' }
    ),
};

// ── Pipeline status types ────────────────────────────────────

export interface PlannedSlot {
  slot_id: number;
  channel_id: number;
  scheduled_at: string;
  target_upload_at?: string;
  target_public_at?: string;
  upload_window_start?: number;
  upload_window_end?: number;
  date_key?: string;
  slot_position: number;
  source_mode: string;
  planned_at?: string;        // fecha de lanzamiento de la programación (ps.created_at)
  channel_name: string;
  channel_slug: string;
}

export interface GeneratingVideo {
  video_id: number;
  channel_id: number;
  status: string;
  progress: number;
  progress_phase: string;
  target_public_at: string | null;
  publish_mode: string;
  created_at: string;
  generation_started_at: string | null;
  generation_finished_at?: string | null;
  plan_start?: string | null;         // inicio creación planificado (slot)
  plan_upload?: string | null;        // subida prevista (slot)
  planned_at?: string | null;         // lanzamiento programación
  source_mode?: string;
  job_id: number | null;
  job_status: string | null;
  job_progress: number | null;
  job_phase: string | null;
  channel_name: string;
  channel_slug: string;
  is_marathon?: number | boolean;
}

export interface AwaitingUploadVideo {
  video_id: number;
  channel_id: number;
  status: string;           // 'awaiting_upload' | 'uploading'
  titulo_final: string | null;
  target_public_at: string | null;
  scheduled_upload_at: string | null;
  target_upload_at: string | null;
  publish_mode: string;
  progress: number;
  progress_phase: string | null;
  created_at: string;
  generation_finished_at: string | null;
  generation_started_at?: string | null;
  plan_start?: string | null;
  planned_at?: string | null;
  source_mode?: string;
  channel_name: string;
  channel_slug: string;
  is_marathon?: number | boolean;
}

export interface WarmingVideo {
  video_id: number;
  channel_id: number;
  status: string;
  privacy_status: string;
  yt_video_id: string;
  titulo_final: string;
  target_public_at: string;
  uploaded_at: string;
  publish_mode: string;
  peak_source: string;
  auto_playlist_id: number | null;
  auto_playlist_name: string | null;
  channel_name: string;
  channel_slug: string;
  held?: boolean;
  created_at?: string;
  generation_started_at?: string | null;
  generation_finished_at?: string | null;
  plan_start?: string | null;
  plan_upload?: string | null;
  planned_at?: string | null;
  source_mode?: string;
  is_marathon?: number | boolean;
}

export interface ShortsPipelineSlot {
  slot_id: number;
  channel_id: number;
  date_key: string;
  scheduled_at: string;
  target_upload_at: string | null;
  short_type: string;   // 'native' | 'clip'
  slot_position: number;
  long_slot_position: number | null;
  source_video_id: number | null;
  status: string;       // 'pending' | 'running'
  job_id: number | null;
  job_status: string | null;
  job_progress: number | null;
  job_phase: string | null;
  actual_completed_at: string | null;  // Real publish time (from shorts table)
  planned_at?: string;                 // lanzamiento programación (sps.created_at)
  source_mode?: string;
  real_start?: string | null;          // inicio real (shorts.created_at aprox)
  real_publish?: string | null;        // publicación real (shorts.published_at)
  plan_upload?: string | null;         // subida prevista (s.publish_at en completed)
  short_id?: number | null;           // v25: for pre-rendered clips
  title?: string | null;              // v25: short title (for ready clips)
  file_path?: string | null;          // v25: local MP4 path (for ready clips)
  short_status?: string | null;       // v25: shorts.status ('ready' for pre-rendered)
  channel_name: string;
  channel_slug: string;
}

export interface PublishedItem {
  id: number;
  channel_id: number;
  channel_name: string;
  channel_slug: string;
  title: string | null;
  youtube_id: string | null;
  published_at: string;
  content_type: 'video' | 'native' | 'clip';
  planned_at?: string | null;
  plan_start?: string | null;
  plan_upload?: string | null;
  plan_publish?: string | null;
  real_start?: string | null;
  real_upload?: string | null;
  source_mode?: string;
  is_marathon?: number | boolean;
}

/** Format seconds to mm:ss */
export function formatDuration(seconds: number): string {
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${m}:${s.toString().padStart(2, '0')}`;
}

/**
 * Parse a timestamp returned by the API.
 *
 * SQLite timestamps are UTC but have no zone marker. Add Z only for those
 * naive values; timestamps that already carry Z or an offset are preserved.
 */
export function parseApiDate(dateValue: string | null | undefined): Date | null {
  if (!dateValue) return null;
  const raw = dateValue.trim();
  if (!raw) return null;

  const hasExplicitZone = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(raw);
  const normalized = raw.includes(' ') ? raw.replace(' ', 'T') : raw;
  const candidate = hasExplicitZone
    ? normalized
    : /^\d{4}-\d{2}-\d{2}$/.test(normalized)
      ? `${normalized}T00:00:00Z`
      : `${normalized}Z`;
  const parsed = new Date(candidate);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

export const API_TIME_ZONE = 'Europe/Madrid';

/** Format a parsed API timestamp in the panel's fixed timezone. */
export function formatApiDate(
  dateValue: string | null,
  options: Intl.DateTimeFormatOptions,
): string {
  const date = parseApiDate(dateValue);
  return date ? date.toLocaleString('es-ES', { ...options, timeZone: API_TIME_ZONE }) : '-';
}

/** Format date string in Europe/Madrid, regardless of browser timezone. */
export function formatDate(dateStr: string | null): string {
  return formatApiDate(dateStr, { day: '2-digit', month: '2-digit', year: 'numeric' });
}

/** Format date + time in Europe/Madrid, regardless of browser timezone. */
export function formatDateTime(dateStr: string | null): string {
  return formatApiDate(dateStr, { day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit' });
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

/** Format big numbers with locale separators (e.g. 1547 → 1.547, 1500000 → 1.500.000) */
export function formatShortNumber(num: string | number): string {
  const n = typeof num === 'string' ? parseInt(num, 10) : num;
  if (isNaN(n)) return '0';
  return n.toLocaleString('es-ES');
}

/** Get status badge class */
export function statusBadge(status: string): string {
  const map: Record<string, string> = {
    draft: 'badge-draft',
    generating: 'badge-generating',
    reassembling: 'badge-reassembling',
    ready: 'badge-ready',
    awaiting_upload: 'badge-awaiting',
    uploading: 'badge-uploading',
    uploaded: 'badge-uploaded',
    error: 'badge-error',
    uploaded_private: 'badge-uploaded_private',
    warming: 'badge-warming',
    scheduled: 'badge-scheduled',
    published: 'badge-published',
    stuck_processing: 'badge-stuck',
    awaiting_script: 'badge-draft',
    cancelled: 'badge-error',
    unlisted: 'badge-uploaded_private',
    private_quality_issue: 'badge-error',
    held: 'badge-error',
    retrying: 'badge-generating',
    deferred: 'badge-awaiting',
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
    awaiting_upload: 'Pendiente subida',
    uploading: 'Subiendo...',
    uploaded: 'Subido',
    error: 'Error',
    uploaded_private: 'Subido (privado)',
    warming: 'Calentando',
    scheduled: 'Programado',
    published: 'Publicado',
    stuck_processing: 'Processing stuck (YT)',
    awaiting_script: 'Esperando script',
    cancelled: 'Cancelado',
    unlisted: 'No listado',
    private_quality_issue: 'Error calidad',
    held: 'Retenido (revisar)',
    retrying: 'Reintentando',
    deferred: 'Diferido (pacing/cuota)',
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
    const target = parseApiDate(targetIso);
    if (!target) return '';
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

/** Format target time as HH:MM in Europe/Madrid with jitter indicator */
export function formatTargetTime(targetIso: string | null): string {
  if (!targetIso) return '';
  try {
    const d = parseApiDate(targetIso);
    if (!d) return '';
    return d.toLocaleTimeString('es-ES', { hour: '2-digit', minute: '2-digit', timeZone: API_TIME_ZONE });
  } catch {
    return '';
  }
}

/** Format an API timestamp as a Madrid-local clock time. */
export function formatTime(dateStr: string | null): string {
  return formatApiDate(dateStr, { hour: '2-digit', minute: '2-digit' });
}

export function madridDateKey(value: string | Date = new Date()): string {
  const date = value instanceof Date ? value : parseApiDate(value);
  if (!date) return '';
  const parts = new Intl.DateTimeFormat('en-CA', { timeZone: API_TIME_ZONE, year: 'numeric', month: '2-digit', day: '2-digit' }).formatToParts(date);
  const get = (type: string) => parts.find(p => p.type === type)?.value || '';
  return `${get('year')}-${get('month')}-${get('day')}`;
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
  target_playlist_name: string | null;
  remaining_seconds: number;
  pending_altered: number;
  pending_endscreens: number;
  yt_video_id: string | null;
  yt_url: string | null;
  uploaded_at: string | null;
}

// ═══════════════════════════════════════════════════════════════
// Timing Dashboard (v11)
// ═══════════════════════════════════════════════════════════════

export interface TimingDashboardResponse {
  ok: boolean
  channel_id: number
  channel_name: string
  config: TimingConfig
  optimal_slots: {
    long: OptimalSlot[]
    shorts: OptimalSlot[]
    has_data: boolean
  }
  execution_history: ExecutionEvent[]
  stats: TimingStats
}

export interface TimingConfig {
  publish_mode: string
  publish_target_hour: number | null
  publish_jitter_min: number
  publish_warmup_min: number
  publish_timezone: string
  publish_window_spread_min: number
  upload_windows: { start: number; end: number }[]
  generation_lead_hours: number
}

export interface OptimalSlot {
  rank: number
  target_hour: number
  target_minute: number
  timezone: string
  score: number
  confidence: number
  audience_focus: string
  calculated_at: string | null
  used_count: number
  avg_views_result: number
  data_sources: Record<string, boolean>
}

export interface ExecutionEvent {
  video_id: number
  titulo_final: string
  is_short: boolean
  status: string
  uploaded_at: string | null
  target_public_at: string | null
  published_at: string | null
  publish_mode: string
  peak_source: string | null
}

export interface TimingStats {
  total_published: number
  total_scheduled: number
  avg_warmup_actual_min: number | null
  pct_within_window: number
}
