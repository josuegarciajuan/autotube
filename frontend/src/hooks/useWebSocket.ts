/** WebSocket hook for real-time generation progress.
 *  v2.4: Auto-reconnect with exponential backoff + console debug logging.
 */
import { useEffect, useRef, useState, useCallback } from 'react';

export interface ProgressData {
  job_id: number;
  status: 'running' | 'completed' | 'failed';
  progress: number;
  phase: string;
  message: string;
  video_id?: number;
  sub_phase?: string;
  detail?: string;
  current?: number;
  total?: number;
  preview_url?: string;
}

const MAX_RECONNECT_DELAY = 30000; // 30s max between reconnect attempts
const INITIAL_RECONNECT_DELAY = 1000; // 1s first attempt
const PING_INTERVAL = 25000;
const STALE_AFTER_MS = 20000; // 20s without WS message → treat as stale, re-enable poll

export function useGenerationProgress(jobId: number | null) {
  const [progress, setProgress] = useState<ProgressData | null>(null);
  const [connected, setConnected] = useState(false);
  const [stale, setStale] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);
  const pingRef = useRef<ReturnType<typeof setInterval>>();
  const reconnectRef = useRef<ReturnType<typeof setTimeout>>();
  const attemptRef = useRef(0);
  const mountedRef = useRef(true);
  const lastUpdateRef = useRef<number>(0);

  const connect = useCallback(() => {
    if (!jobId || !mountedRef.current) return;

    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${protocol}//${window.location.host}/ws/progress/${jobId}`;

    console.log(`[Autotube WS] Connecting to ${wsUrl} (attempt #${attemptRef.current + 1})`);

    const ws = new WebSocket(wsUrl);
    wsRef.current = ws;

    ws.onopen = () => {
      if (!mountedRef.current) { ws.close(); return; }
      console.log('[Autotube WS] Connected');
      setConnected(true);
      setStale(false);
      lastUpdateRef.current = Date.now();
      attemptRef.current = 0; // reset backoff on successful connection
    };

    ws.onclose = (event) => {
      if (!mountedRef.current) return;
      console.log(`[Autotube WS] Disconnected (code=${event.code}, reason=${event.reason || 'none'})`);
      setConnected(false);

      // Don't reconnect if we completed/failed
      if (event.code === 1000) {
        console.log('[Autotube WS] Normal close, no reconnect');
        return;
      }

      // Exponential backoff reconnect
      const delay = Math.min(
        INITIAL_RECONNECT_DELAY * Math.pow(2, attemptRef.current),
        MAX_RECONNECT_DELAY
      );
      attemptRef.current++;
      console.log(`[Autotube WS] Reconnecting in ${delay}ms...`);
      reconnectRef.current = setTimeout(connect, delay);
    };

    ws.onerror = (event) => {
      console.error('[Autotube WS] Error:', event);
      // onclose will fire after onerror, so reconnection happens there
    };

    ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data) as ProgressData;
        setProgress(data);
        setStale(false);
        lastUpdateRef.current = Date.now();
        if (data.status === 'completed' || data.status === 'failed') {
          console.log(`[Autotube WS] Job ${data.job_id} ${data.status}, closing`);
          ws.close(1000);
        }
      } catch (e) {
        console.warn('[Autotube WS] Failed to parse message:', event.data);
      }
    };
  }, [jobId]);

  useEffect(() => {
    mountedRef.current = true;

    if (!jobId) {
      setProgress(null);
      setConnected(false);
      setStale(false);
      return;
    }

    attemptRef.current = 0;
    connect();

    // Keep-alive ping
    pingRef.current = setInterval(() => {
      if (wsRef.current?.readyState === WebSocket.OPEN) {
        wsRef.current.send('ping');
      }
    }, PING_INTERVAL);

    return () => {
      mountedRef.current = false;
      clearInterval(pingRef.current);
      clearTimeout(reconnectRef.current);
      if (wsRef.current) {
        wsRef.current.onclose = null; // prevent reconnect on unmount
        wsRef.current.close();
      }
    };
  }, [jobId, connect]);

  // ── Staleness watchdog (separate effect so it doesn't reset the WS) ──
  // If the WS stays open but stops delivering messages (API restarted and
  // the monitor task died), the bar would freeze. After STALE_AFTER_MS of
  // silence we flip `stale` so the polling fallback re-engages.
  useEffect(() => {
    if (!jobId || !connected) return;
    const watchdog = setInterval(() => {
      if (mountedRef.current && lastUpdateRef.current > 0
          && Date.now() - lastUpdateRef.current > STALE_AFTER_MS) {
        console.warn('[Autotube WS] No message for >20s — switching to poll fallback');
        setStale(true);
      }
    }, 5000);
    return () => clearInterval(watchdog);
  }, [jobId, connected]);

  // ── Polling fallback: always do an initial fetch, then poll every 3s when WS is disconnected ──
  // v2.4.1: added max retries to prevent infinite polling on zombie jobs
  useEffect(() => {
    if (!jobId) return;

    const safeJobId: number = jobId; // narrow after guard
    let active = true;
    let pollTimer: ReturnType<typeof setTimeout>;
    let pollCount = 0;
    const MAX_POLLS = 30; // ~90 seconds at 3s intervals (plus 1s initial = ~91s)
    let lastNonZeroProgress = false;

    async function poll() {
      if (!active) return;
      pollCount++;
      try {
        const res = await fetch(`api/jobs/${safeJobId}`);
        if (!res.ok) {
          // If the job is 404, stop polling (job was deleted/cleaned up)
          if (res.status === 404 && active) {
            setProgress({
              job_id: safeJobId,
              status: 'completed',
              progress: 100,
              phase: '(auto-cleaned)',
              message: 'Trabajo eliminado del servidor',
            });
            return;
          }
          if ((!connected || stale) && pollCount < MAX_POLLS) {
            pollTimer = setTimeout(poll, 3000);
          }
          return;
        }
        const job = await res.json();
        const data: ProgressData = {
          job_id: job.id ?? safeJobId,
          status: job.status ?? 'running',
          progress: job.progress ?? 0,
          phase: job.phase ?? '',
          message: job.error_msg ? `Error: ${job.error_msg}` : (job.phase ? `Fase: ${job.phase}` : 'Generando...'),
          sub_phase: job.sub_phase,
          detail: job.detail,
          current: job.progress_current ?? undefined,
          total: job.progress_total ?? undefined,
        };
        if (active) {
          setProgress(prev => {
            // Only update if the polled progress is newer/greater
            if (!prev || data.progress >= (prev.progress ?? 0)) return data;
            return prev;
          });
          // A live poll response proves we're getting data → not stale anymore
          lastUpdateRef.current = Date.now();
        }
        if (data.progress > 0) lastNonZeroProgress = true;
        if (data.status === 'completed' || data.status === 'failed') {
          return; // stop polling
        }
        // Stale job detection: after max polls with 0% progress → auto-dismiss
        if (!lastNonZeroProgress && pollCount >= MAX_POLLS && active) {
          setProgress({
            job_id: safeJobId,
            status: 'completed',
            progress: 100,
            phase: '(timeout)',
            message: 'Sin progreso en ~90s — auto-cerrando',
          });
          return;
        }
      } catch {
        // ignore network errors, retry only if disconnected and not exhausted
        if (!active || pollCount >= MAX_POLLS) return;
      }
      // Only schedule next poll if WS is disconnected/stale and we haven't hit max
      if (active && (!connected || stale) && pollCount < MAX_POLLS) {
        pollTimer = setTimeout(poll, 3000);
      }
    }

    pollTimer = setTimeout(poll, 1000); // first poll after 1s delay always runs

    return () => {
      active = false;
      clearTimeout(pollTimer);
    };
  }, [jobId, connected, stale]);

  const reset = useCallback(() => {
    setProgress(null);
    setConnected(false);
    setStale(false);
    attemptRef.current = 0;
    lastUpdateRef.current = 0;
  }, []);

  return { progress, connected, stale, reset };
}
