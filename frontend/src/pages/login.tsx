import { useState, type FormEvent } from 'react'
import { Navigate } from 'react-router'
import { useAuth } from '@/auth'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { BookOpen, ArrowRight, Check, Network } from 'lucide-react'

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
    <main className="login-page">
      <section className="login-story" aria-label="真懂 AI 教学理念">
        <div className="login-brand">
          <BookOpen aria-hidden /> 真懂 AI
        </div>
        <div className="login-story-body">
          <p className="login-subject">高等数学自主教学</p>
          <h1>
            从做对一道题，
            <br />
            到真正理解。
          </h1>
          <p className="login-intro">
            发现错因，针对性辅导，再用独立验证确认掌握。让学习的每一步，都有据可循。
          </p>
          <div
            className="learning-diagram"
            aria-label="发现问题、诊断辅导、独立验证的教学闭环"
          >
            <div>
              <Network aria-hidden />
              <span>发现问题</span>
              <small>从作答中识别薄弱点</small>
            </div>
            <ArrowRight className="flow-arrow" aria-hidden />
            <div>
              <BookOpen aria-hidden />
              <span>诊断辅导</span>
              <small>追问并检验错因假设</small>
            </div>
            <ArrowRight className="flow-arrow" aria-hidden />
            <div>
              <Check aria-hidden />
              <span>独立验证</span>
              <small>用新题确认真正掌握</small>
            </div>
          </div>
        </div>
        <p className="login-footnote">教师看见证据，学生获得及时的帮助。</p>
      </section>
      <section className="login-entry">
        <form onSubmit={submit} className="login-form">
          <div className="mb-8">
            <h2 className="text-2xl font-semibold">进入教学工作台</h2>
            <p className="text-muted-foreground mt-3 text-sm">
              使用教师或学生账号，继续你的教学与学习。
            </p>
          </div>
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
            {busy ? '正在登录…' : '登录工作台'}
          </Button>
          <p className="text-muted-foreground text-xs leading-6">
            账号由教师或管理员分配。学生登录后仅可查看本人的学习任务与进度。
          </p>
        </form>
      </section>
    </main>
  )
}
