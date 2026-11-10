import { useState, type FormEvent } from 'react'
import { Navigate } from 'react-router'
import { useAuth } from '@/auth'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'

export default function Login() {
  const { user, login } = useAuth()
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  if (user)
    return (
      <Navigate
        to={user.role === 'teacher' ? '/teacher' : '/student'}
        replace
      />
    )
  async function submit(event: FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError('')
    try {
      await login(username, password)
    } catch {
      setError('登录失败，请检查账号、密码和网络连接')
    } finally {
      setBusy(false)
    }
  }
  return (
    <main className="bg-background flex min-h-svh items-center justify-center p-4">
      <form
        onSubmit={submit}
        className="w-full max-w-sm space-y-4 rounded-lg border p-6"
      >
        <h1 className="text-xl font-semibold">登录真懂 AI</h1>
        <label className="block space-y-1 text-sm">
          账号
          <Input
            required
            autoComplete="username"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
          />
        </label>
        <label className="block space-y-1 text-sm">
          密码
          <Input
            required
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </label>
        {error && (
          <p role="alert" className="text-destructive text-sm">
            {error}
          </p>
        )}
        <Button className="w-full" disabled={busy} type="submit">
          登录
        </Button>
      </form>
    </main>
  )
}
