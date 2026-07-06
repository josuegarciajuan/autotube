/** Global context to track active video generation jobs across pages.
 *  v2.6: Discovers active jobs from API on mount + migrates old storage key.
 *  Persists to localStorage so progress survives page refreshes.
 */

import { createContext, useContext, useState, useCallback, useEffect, type ReactNode } from 'react'

export interface ActiveJob {
  jobId: number
  channelId: number
  channelName: string
  action: string
  videoId?: number
}

const STORAGE_KEY = 'autotube_active_jobs_v2'
const OLD_STORAGE_KEY = 'autotube_active_job'

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
    // Try new key first
    const raw = localStorage.getItem(STORAGE_KEY)
    if (raw) {
      const parsed = JSON.parse(raw)
      if (Array.isArray(parsed)) {
        return parsed.filter(j => j && typeof j.jobId === 'number')
      }
    }
    // Migrate from old key (single job → array)
    const old = localStorage.getItem(OLD_STORAGE_KEY)
    if (old) {
      const parsed = JSON.parse(old)
      if (parsed && typeof parsed.jobId === 'number') {
        const migrated = [parsed as ActiveJob]
        // Save to new key so next load uses it
        localStorage.setItem(STORAGE_KEY, JSON.stringify(migrated))
        return migrated
      }
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

  const addJob = useCallback((job: ActiveJob) => {
    setActiveJobs(prev => {
      const filtered = prev.filter(j => j.jobId !== job.jobId)
      const next = [...filtered, job]
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

      // 2. Fetch channel names (needed for API-discovered jobs)
      const channelNames = new Map<number, string>()
      try {
        const chRes = await fetch('api/channels')
        if (chRes.ok) {
          const channels = await chRes.json()
          for (const ch of channels) {
            channelNames.set(ch.id, ch.name || ch.slug || `Canal ${ch.id}`)
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

            // Already in stored? Use stored version (has user-set channelName)
            if (storedMap.has(jobId)) {
              result.push(storedMap.get(jobId)!)
              continue
            }

            // New job from API: build ActiveJob
            const chName = channelNames.get(j.channel_id) || `Canal ${j.channel_id}`
            result.push({
              jobId,
              channelId: j.channel_id,
              channelName: chName,
              action: j.action || 'generate_and_upload',
              videoId: j.video_id,
            })
          }

          // 4. Remove completed/failed jobs (not in active list)
          // This handles the case where a job completed but we didn't catch it
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

  return (
    <GenerationContext.Provider value={{ activeJobs, addJob, removeJob, clearAll, isChannelBusy }}>
      {children}
    </GenerationContext.Provider>
  )
}

export function useGeneration() {
  return useContext(GenerationContext)
}
