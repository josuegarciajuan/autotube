import { lazy, Suspense } from 'react'
import { Routes, Route } from 'react-router-dom'
import Shell from './components/layout/Shell'
import { GenerationProvider } from './context/GenerationContext'
import { ChannelFilterProvider } from './context/ChannelFilterContext'
import { EasterEggProvider } from './context/EasterEggContext'
import { QueryProvider } from './context/QueryProvider'
import GenerationProgressBar from './components/GenerationProgressBar'

// Lazy-load page components for code splitting
const Dashboard = lazy(() => import('./pages/Dashboard'))
const Channels = lazy(() => import('./pages/Channels'))
const ChannelDetail = lazy(() => import('./pages/ChannelDetail'))
const VideoEditor = lazy(() => import('./pages/VideoEditor'))
const Scheduling = lazy(() => import('./pages/Scheduling'))
const Monitor = lazy(() => import('./pages/Monitor'))
const Distribution = lazy(() => import('./pages/Distribution'))

function PageLoader() {
  return (
    <div className="flex items-center justify-center h-64">
      <div className="w-8 h-8 border-2 border-indigo-500 border-t-transparent rounded-full animate-spin" />
    </div>
  )
}

export default function App() {
  return (
    <QueryProvider>
      <EasterEggProvider>
        <ChannelFilterProvider>
          <GenerationProvider>
            <Suspense fallback={<PageLoader />}>
              <Routes>
                <Route element={<Shell />}>
                  <Route path="/" element={<Dashboard />} />
                  <Route path="/channels" element={<Channels />} />
                  <Route path="/channels/:id" element={<ChannelDetail />} />
                  <Route path="/videos/:id/edit" element={<VideoEditor />} />
                  <Route path="/scheduling" element={<Scheduling />} />
                  <Route path="/monitor" element={<Monitor />} />
                  <Route path="/distribution" element={<Distribution />} />
                </Route>
              </Routes>
            </Suspense>
            <GenerationProgressBar />
          </GenerationProvider>
        </ChannelFilterProvider>
      </EasterEggProvider>
    </QueryProvider>
  )
}
