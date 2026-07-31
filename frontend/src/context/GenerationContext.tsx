/** Global context to track active video generation jobs across pages.
 *  v3.0: Uses React Query for polling instead of manual setInterval.
 *  Persists to localStorage so progress survives page refreshes.
 */

import { createContext, useContext, useState, useCallback, useEffect, useRef, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '../lib/api'

export interface ActiveJob {
  jobId: number
  channelId: number
  channelName: string
  action: string
  videoId?: number
  storedAt?: number // timestamp to detect stale/zombie jobs
}

const STORAGE_KEY = 'autotube_active_jobs_v2'
const OLD_STORAGE_KEY = 'autotube_active_job'
const MAX_JOB_AGE_MS = 30 * 60 * 1000 // 30 min — jobs older than this are stale zombies

interface GenerationContextType {
  activeJobs: ActiveJob[]
  addJob: (job: ActiveJob) => void
  removeJob: (jobId: number) => void
  clearAll: () => void
  isChannelBusy: (channelId: number) => boolean
}

const GenerationContext = createContext<GenerationContextType>({
  activeJobs: [],
  addJob: () => {},
  removeJob: () => {},
  clearAll: () => {},
  isChannelBusy: () => false,
})

function loadFromStorage(): ActiveJob[] {
  try {
    const now = Date.now()
    // Try new key first
    const raw = localStorage.getItem(STORAGE_KEY)
    if (raw) {
      const parsed = JSON.parse(raw)
      if (Array.isArray(parsed)) {
        const filtered = parsed.filter(
          j => j && typeof j.jobId === 'number'
            && (!j.storedAt || (now - j.storedAt) < MAX_JOB_AGE_MS)
        )
        // If we filtered out stale jobs, persist the cleaned list
        if (filtered.length !== parsed.length) {
          saveToStorage(filtered)
        }
        return filtered
      }
    }
    // Migrate from old key (single job → array)
    const old = localStorage.getItem(OLD_STORAGE_KEY)
    if (old) {
      const parsed = JSON.parse(old)
      if (parsed && typeof parsed.jobId === 'number') {
        const migrated = [parsed as ActiveJob]
        migrated[0].storedAt = now
        // Only keep if not too old
        if (!parsed.storedAt || (now - parsed.storedAt) < MAX_JOB_AGE_MS) {
          saveToStorage(migrated)
          return migrated
        }
      }
      // Old key had a stale job — clean it up
      localStorage.removeItem(OLD_STORAGE_KEY)
    }
  } catch {}
  return []
}

function saveToStorage(jobs: ActiveJob[]) {
  try {
    if (jobs.length > 0) {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(jobs))
    } else {
      localStorage.removeItem(STORAGE_KEY)
    }
  } catch {}
}

export function GenerationProvider({ children }: { children: ReactNode }) {
  const [activeJobs, setActiveJobs] = useState<ActiveJob[]>(loadFromStorage)
  const channelNamesRef = useRef<Map<number, string>>(new Map())

  // React Query: fetch active jobs every 15s (replaces manual setInterval)
  const { data: apiJobs } = useQuery({
    queryKey: ['active-jobs'],
    queryFn: () => api.getActiveJobs(),
    refetchInterval: 15_000,
    staleTime: 10_000,
  })

  // React Query: fetch channel names once (replaces manual fetch in useEffect)
  const { data: channels } = useQuery({
    queryKey: ['channels', true],
    queryFn: () => api.getChannels(true),
    staleTime: 300_000, // 5 min — channels rarely change
  })

  // Update channel names ref when channels load
  useEffect(() => {
    if (channels) {
      for (const ch of channels) {
        channelNamesRef.current.set(ch.id, ch.name || ch.slug || `Canal ${ch.id}`)
      }
    }
  }, [channels])

  // Sync API jobs into context state (replaces the 200-line discoverAndVerify + pollForNewJobs)
  useEffect(() => {
    if (!apiJobs || apiJobs.length === 0) return

    const apiJobIds = new Set<number>(apiJobs.map((j: any) => j.id))

    setActiveJobs(prev => {
      let changed = false
      const result: ActiveJob[] = []

      // Keep existing jobs that are still active, refresh names
      const storedMap = new Map<number, ActiveJob>()
      for (const j of prev) storedMap.set(j.jobId, j)

      for (const j of apiJobs) {
        const jobId = j.id
        const existing = storedMap.get(jobId)
        if (existing) {
          const freshName = channelNamesRef.current.get(j.channel_id)
          if (freshName && existing.channelName !== freshName) {
            existing.channelName = freshName
            changed = true
          }
          result.push(existing)
        } else {
          // New job from API
          result.push({
            jobId,
            channelId: j.channel_id,
            channelName: channelNamesRef.current.get(j.channel_id) || `Canal ${j.channel_id}`,
            action: j.action || 'generate_and_upload',
            videoId: j.video_id,
            storedAt: Date.now(),
          })
          changed = true
        }
      }

      if (changed || result.length !== prev.length) {
        saveToStorage(result)
        return result
      }
      return prev
    })
  }, [apiJobs])

  // Mutation functions (called imperatively by dispatch buttons)
  const addJob = useCallback((job: ActiveJob) => {
    setActiveJobs(prev => {
      const filtered = prev.filter(j => j.jobId !== job.jobId)
      const next = [...filtered, { ...job, storedAt: Date.now() }]
      saveToStorage(next)
      return next
    })
  }, [])

  const removeJob = useCallback((jobId: number) => {
    setActiveJobs(prev => {
      const next = prev.filter(j => j.jobId !== jobId)
      saveToStorage(next)
      return next
    })
  }, [])

  const clearAll = useCallback(() => {
    setActiveJobs([])
    saveToStorage([])
  }, [])

  const isChannelBusy = useCallback((channelId: number) => {
    return activeJobs.some(j => j.channelId === channelId)
  }, [activeJobs])

  return (
    <GenerationContext.Provider value={{ activeJobs, addJob, removeJob, clearAll, isChannelBusy }}>
      {children}
    </GenerationContext.Provider>
  )
}

export function useGeneration() {
  return useContext(GenerationContext)
}
