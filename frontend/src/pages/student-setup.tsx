import { useEffect, useState, type FormEvent } from 'react'
import { Link, useNavigate } from 'react-router'
import { api } from '@/api/client'
import { useAuth } from '@/auth'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'

export default function StudentSetup() {
  const [token] = useState(() => window.location.hash.slice(1))
  const [password, setPassword] = useState('')
  const [again, setAgain] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const setup = api.useMutation('post', '/api/auth/student-setup', {
    gcTime: 0,
  })
  const { login } = useAuth()
  const navigate = useNavigate()

  useEffect(() => {
    if (window.location.hash) {
      window.history.replaceState(
        window.history.state,
        '',
        window.location.pathname,
      )
    }
  }, [])

  async function submit(event: FormEvent) {
    event.preventDefault()
    if (password !== again) {
      setError('两次输入的密码不一致。')
      return
    }
    setError('')
    setBusy(true)
    try {
      const account = await setup.mutateAsync({ body: { token, password } })
      setPassword('')
      setAgain('')
      try {
        await login(account.username, password)
        void navigate('/student', { replace: true })
      } catch {
        setError(`密码已设置。请用账号 ${account.username} 前往登录。`)
      }
    } catch (failure) {
      setError(
        failure instanceof Error ? failure.message : '无法设置密码，请重试。',
      )
    } finally {
      setBusy(false)
      setup.reset()
    }
  }

  return (
    <main className="grid min-h-dvh place-items-center p-4">
      <div className="grid w-full max-w-md gap-5 rounded-xl border bg-card p-6">
        <h1 className="text-2xl font-semibold">设置学生密码</h1>
        {token ? (
          <form className="grid gap-3" onSubmit={submit}>
            <p className="text-muted-foreground text-sm">
              设置至少 12 个字符的密码。设置成功后会进入学生辅导台。
            </p>
            <Label htmlFor="setup-password">新密码</Label>
            <Input
              id="setup-password"
              type="password"
              required
              minLength={12}
              maxLength={128}
              autoComplete="new-password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
            />
            <Label htmlFor="setup-again">确认新密码</Label>
            <Input
              id="setup-again"
              type="password"
              required
              minLength={12}
              maxLength={128}
              autoComplete="new-password"
              value={again}
              onChange={(event) => setAgain(event.target.value)}
            />
            <Button disabled={busy}>设置密码并登录</Button>
          </form>
        ) : (
          <p>请打开教师发给你的一次性设置链接。</p>
        )}
        {error && (
          <p role="alert" className="text-destructive text-sm">
            {error}
          </p>
        )}
        <Link to="/login" className="text-sm underline">
          返回登录
        </Link>
      </div>
    </main>
  )
}
