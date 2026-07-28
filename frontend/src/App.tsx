import { Routes, Route } from 'react-router-dom'
import Shell from './components/layout/Shell'
import Dashboard from './pages/Dashboard'
import Channels from './pages/Channels'
import ChannelDetail from './pages/ChannelDetail'
import VideoEditor from './pages/VideoEditor'
import Scheduling from './pages/Scheduling'
import Monitor from './pages/Monitor'
import { GenerationProvider } from './context/GenerationContext'
import { ChannelFilterProvider } from './context/ChannelFilterContext'
import { EasterEggProvider } from './context/EasterEggContext'
import GenerationProgressBar from './components/GenerationProgressBar'

export default function App() {
  return (
    <EasterEggProvider>
      <ChannelFilterProvider>
        <GenerationProvider>
          <Routes>
            <Route element={<Shell />}>
              <Route path="/" element={<Dashboard />} />
              <Route path="/channels" element={<Channels />} />
              <Route path="/channels/:id" element={<ChannelDetail />} />
              <Route path="/videos/:id/edit" element={<VideoEditor />} />
              <Route path="/scheduling" element={<Scheduling />} />
              <Route path="/monitor" element={<Monitor />} />
            </Route>
          </Routes>
          <GenerationProgressBar />
        </GenerationProvider>
      </ChannelFilterProvider>
    </EasterEggProvider>
  )
}
