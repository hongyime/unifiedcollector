import { Routes, Route, Navigate } from 'react-router-dom'
import Sidebar from './components/Layout/Sidebar'
import Login from './pages/Login'
import SystemHealth from './pages/SystemHealth'
import Collector from './pages/Collector'
import MediaArchival from './pages/MediaArchival'
import FaceRecognition from './pages/FaceRecognition'
import UserIntelligence from './pages/UserIntelligence'
import LinkDiscovery from './pages/LinkDiscovery'
import BulkSender from './pages/BulkSender'
import LiveConfig from './pages/LiveConfig'

function Layout() {
  return (
    <div className="flex h-screen overflow-hidden bg-bg-base">
      <Sidebar />
      <main className="flex-1 overflow-y-auto min-w-0">
        <Routes>
          <Route path="/" element={<Navigate to="/health" replace />} />
          <Route path="/health" element={<SystemHealth />} />
          <Route path="/collector" element={<Collector />} />
          <Route path="/media" element={<MediaArchival />} />
          <Route path="/faces" element={<FaceRecognition />} />
          <Route path="/users" element={<UserIntelligence />} />
          <Route path="/links" element={<LinkDiscovery />} />
          <Route path="/bulk" element={<BulkSender />} />
          <Route path="/config" element={<LiveConfig />} />
          <Route path="*" element={<Navigate to="/health" replace />} />
        </Routes>
      </main>
    </div>
  )
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route path="/*" element={<Layout />} />
    </Routes>
  )
}
