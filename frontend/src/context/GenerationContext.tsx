/** Global context to track active video generation jobs across pages.
 *  v2.6: Discovers active jobs from API on mount + migrates old storage key.
 *  Persists to localStorage so progress survives page refreshes.
 */

import { createContext, useContext, useState, useCallback, useEffect, useRef, type ReactNode } from 'react'

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

  // On mount: verify stored jobs + discover active jobs from API
  useEffect(() => {
    let cancelled = false

    async function discoverAndVerify() {
      // 1. Load stored jobs
      const stored = loadFromStorage()
      const storedMap = new Map<number, ActiveJob>()
      for (const j of stored) storedMap.set(j.jobId, j)

      // 2. Fetch channel names (needed for API-discovered jobs + polling)
      try {
        const chRes = await fetch('api/channels')
        if (chRes.ok) {
          const channels = await chRes.json()
          for (const ch of channels) {
            channelNamesRef.current.set(ch.id, ch.name || ch.slug || `Canal ${ch.id}`)
          }
        }
      } catch {}

      // 3. Fetch active jobs from API
      try {
        const jobsRes = await fetch('api/jobs/active')
        if (jobsRes.ok) {
          const apiJobs = await jobsRes.json()
          const result: ActiveJob[] = []

          for (const j of apiJobs) {
            const jobId = j.id

            // Already in stored? Refresh channelName from API if available
            if (storedMap.has(jobId)) {
              const storedJob = storedMap.get(jobId)!
              const freshName = channelNamesRef.current.get(j.channel_id)
              if (freshName) {
                storedJob.channelName = freshName
              }
              result.push(storedJob)
              continue
            }

            // New job from API: build ActiveJob
            const chName = channelNamesRef.current.get(j.channel_id) || `Canal ${j.channel_id}`
            result.push({
              jobId,
              channelId: j.channel_id,
              channelName: chName,
              action: j.action || 'generate_and_upload',
              videoId: j.video_id,
              storedAt: Date.now(),
            })
          }

          // 4. Remove completed/failed jobs (not in active list)
          if (!cancelled) {
            setActiveJobs(result)
            saveToStorage(result)
          }
        } else {
          // API failed → fall back to stored verification
          const stillActive: ActiveJob[] = []
          for (const job of stored) {
            try {
              const res = await fetch(`api/jobs/${job.jobId}`)
              if (!res.ok) {
                if (res.status === 404) continue
                stillActive.push(job)
                continue
              }
              const data = await res.json()
              if (data.status !== 'completed' && data.status !== 'failed') {
                stillActive.push(job)
              }
            } catch {
              stillActive.push(job)
            }
          }
          if (!cancelled) {
            setActiveJobs(stillActive)
            saveToStorage(stillActive)
          }
        }
      } catch {
        // Network error → keep stored jobs as-is
      }
    }

    discoverAndVerify()
    return () => { cancelled = true }
  }, [])

  // Periodic polling: discover new jobs created by schedulers (upload, publish, etc.)
  // Runs every 15 seconds so the bottom bar catches scheduled operations
  useEffect(() => {
    async function pollForNewJobs() {
      try {
        const jobsRes = await fetch('api/jobs/active')
        if (!jobsRes.ok) return
        const apiJobs = await jobsRes.json()
        const apiJobIds = new Set<number>(apiJobs.map((j: any) => j.id))
        
        setActiveJobs(prev => {
          let changed = false
          
          // Remove jobs that are no longer active (completed/failed)
          const filtered = prev.filter(j => apiJobIds.has(j.jobId))
          if (filtered.length !== prev.length) changed = true
          
          // Refresh channel names for existing jobs from the ref
          for (const j of filtered) {
            const freshName = channelNamesRef.current.get(j.channelId)
            if (freshName && j.channelName !== freshName) {
              j.channelName = freshName
              changed = true
            }
          }
          
           // Add new jobs discovered via API
          const existingIds = new Set(filtered.map(j => j.jobId))
          for (const j of apiJobs) {
            if (!existingIds.has(j.id)) {
              filtered.push({
                jobId: j.id,
                channelId: j.channel_id,
                channelName: channelNamesRef.current.get(j.channel_id) || `Canal ${j.channel_id}`,
                action: j.action || 'generate_and_upload',
                videoId: j.video_id,
                storedAt: Date.now(),
              })
              changed = true
            }
          }
          
          if (changed) {
            saveToStorage(filtered)
            return filtered
          }
          return prev
        })
      } catch {
        // Silently fail — keep current jobs
      }
    }
    
    const interval = setInterval(pollForNewJobs, 15_000)
    return () => clearInterval(interval)
  }, [])

  return (
    <GenerationContext.Provider value={{ activeJobs, addJob, removeJob, clearAll, isChannelBusy }}>
      {children}
    </GenerationContext.Provider>
  )
}

export function useGeneration() {
  return useContext(GenerationContext)
}
