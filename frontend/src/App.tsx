import { Routes, Route } from 'react-router-dom'
import Shell from './components/layout/Shell'
import Dashboard from './pages/Dashboard'
import Channels from './pages/Channels'
import ChannelDetail from './pages/ChannelDetail'
import VideoEditor from './pages/VideoEditor'
import Scheduling from './pages/Scheduling'
import { GenerationProvider } from './context/GenerationContext'
import GenerationProgressBar from './components/GenerationProgressBar'

export default function App() {
  return (
    <GenerationProvider>
      <Routes>
        <Route element={<Shell />}>
          <Route path="/" element={<Dashboard />} />
          <Route path="/channels" element={<Channels />} />
          <Route path="/channels/:id" element={<ChannelDetail />} />
          <Route path="/videos/:id/edit" element={<VideoEditor />} />
          <Route path="/scheduling" element={<Scheduling />} />
        </Route>
      </Routes>
      <GenerationProgressBar />
    </GenerationProvider>
  )
}
