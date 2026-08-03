import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30 * 1000,     // data is fresh for 30 seconds
      refetchOnWindowFocus: false,
      retry: 3,
      retryDelay: (attempt) => Math.min(5000 * 2 ** attempt, 20000),
    },
    mutations: {
      retry: 0,
    },
  },
})

export function QueryProvider({ children }: { children: ReactNode }) {
  return (
    <QueryClientProvider client={queryClient}>
      {children}
    </QueryClientProvider>
  )
}
