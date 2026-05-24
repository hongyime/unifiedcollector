import { Routes, Route, Navigate } from 'react-router-dom'
import Sidebar from './components/Layout/Sidebar'
import SystemHealth from './pages/SystemHealth'
import Collector from './pages/Collector'
import FaceRecognition from './pages/FaceRecognition'
import UserIntelligence from './pages/UserIntelligence'
import LinkDiscovery from './pages/LinkDiscovery'
import Config from './pages/Config'

export default function App() {
  return (
    <div className="flex h-screen overflow-hidden bg-bg-base">
      <Sidebar />
      <main className="flex-1 overflow-y-auto min-w-0">
        <Routes>
          <Route path="/" element={<Navigate to="/health" replace />} />
          <Route path="/health" element={<SystemHealth />} />
          <Route path="/collector" element={<Collector />} />
          <Route path="/faces" element={<FaceRecognition />} />
          <Route path="/users" element={<UserIntelligence />} />
          <Route path="/links" element={<LinkDiscovery />} />
          <Route path="/config" element={<Config />} />
        </Routes>
      </main>
    </div>
  )
}
