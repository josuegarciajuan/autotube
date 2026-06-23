/** WebSocket hook for real-time generation progress. */
import { useEffect, useRef, useState, useCallback } from 'react';

export interface ProgressData {
  job_id: number;
  status: string;
  progress: number;
  phase: string;
  message: string;
  video_id?: number;
}

export function useGenerationProgress(jobId: number | null) {
  const [progress, setProgress] = useState<ProgressData | null>(null);
  const [connected, setConnected] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);
  const pingRef = useRef<ReturnType<typeof setInterval>>();

  useEffect(() => {
    if (!jobId) {
      setProgress(null);
      setConnected(false);
      return;
    }

    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${protocol}//${window.location.host}/autotube/ws/progress/${jobId}`;
    
    const ws = new WebSocket(wsUrl);
    wsRef.current = ws;

    ws.onopen = () => setConnected(true);
    ws.onclose = () => setConnected(false);
    
    ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        setProgress(data);
        if (data.status === 'completed' || data.status === 'failed') {
          ws.close();
        }
      } catch {}
    };

    // Keep-alive ping every 25 seconds
    pingRef.current = setInterval(() => {
      if (ws.readyState === WebSocket.OPEN) {
        ws.send('ping');
      }
    }, 25000);

    return () => {
      clearInterval(pingRef.current);
      ws.close();
    };
  }, [jobId]);

  const reset = useCallback(() => {
    setProgress(null);
    setConnected(false);
  }, []);

  return { progress, connected, reset };
}
