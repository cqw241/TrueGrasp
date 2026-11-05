import { useEffect, useState } from 'react'

type BackendStatus = 'checking' | 'ok' | 'unreachable'

function App() {
  const [status, setStatus] = useState<BackendStatus>('checking')

  useEffect(() => {
    fetch('/api/health')
      .then((res) => setStatus(res.ok ? 'ok' : 'unreachable'))
      .catch(() => setStatus('unreachable'))
  }, [])

  return (
    <main style={{ padding: '2rem' }}>
      <h1>真懂 AI</h1>
      <p>后端状态：{status}</p>
    </main>
  )
}

export default App
