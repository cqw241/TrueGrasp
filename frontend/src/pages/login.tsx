import { useState, type FormEvent } from 'react'
import { Navigate } from 'react-router'
import { useAuth } from '@/auth'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { BookOpen, ArrowRight, Check, Network } from 'lucide-react'

export default function Login() {
  const { user, login, registerTeacher } = useAuth()
  const [registering, setRegistering] = useState(false)
  const [again, setAgain] = useState('')
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  if (user)
    return (
      <Navigate
        to={
          user.role === 'teacher'
            ? registering
              ? '/classes'
              : '/teacher'
            : '/student'
        }
        replace
      />
    )
  async function submit(event: FormEvent) {
    event.preventDefault()
    setError('')
    if (registering && password !== again) {
      setError('两次输入的密码不一致')
      return
    }
    setBusy(true)
    try {
      if (registering) await registerTeacher(username.trim(), password)
      else await login(username, password)
    } catch (failure) {
      setError(
        failure instanceof Error ? failure.message : '操作失败，请稍后重试',
      )
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
        <form onSubmit={submit} className="login-form" aria-busy={busy}>
          <div className="mb-8">
            <h2 className="text-2xl font-semibold">
              {registering ? '注册教师账号' : '进入教学工作台'}
            </h2>
            <p className="text-muted-foreground mt-3 text-sm">
              {registering
                ? '注册后可创建班级、添加学生并发布作业。'
                : '使用教师或学生账号，继续你的教学与学习。'}
            </p>
          </div>
          <label className="block space-y-1 text-sm">
            账号
            <Input
              required
              autoComplete="username"
              maxLength={128}
              disabled={busy}
              value={username}
              onChange={(e) => setUsername(e.target.value)}
            />
          </label>
          <label className="block space-y-1 text-sm">
            密码
            <Input
              required
              type="password"
              autoComplete={registering ? 'new-password' : 'current-password'}
              minLength={registering ? 12 : 1}
              maxLength={registering ? 128 : 1024}
              disabled={busy}
              aria-describedby={
                registering ? 'registration-password-help' : undefined
              }
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </label>
          {registering && (
            <>
              <p
                id="registration-password-help"
                className="text-muted-foreground text-xs"
              >
                密码需为 12～128 个字符。
              </p>
              <label className="block space-y-1 text-sm">
                确认密码
                <Input
                  required
                  type="password"
                  autoComplete="new-password"
                  minLength={12}
                  maxLength={128}
                  disabled={busy}
                  value={again}
                  onChange={(e) => setAgain(e.target.value)}
                />
              </label>
            </>
          )}
          {error && (
            <p role="alert" className="text-destructive text-sm">
              {error}
            </p>
          )}
          <Button className="w-full" disabled={busy} type="submit">
            {registering
              ? busy
                ? '正在注册…'
                : '注册并进入班级管理'
              : busy
                ? '正在登录…'
                : '登录工作台'}
          </Button>
          <Button
            type="button"
            variant="link"
            className="w-full"
            disabled={busy}
            onClick={() => {
              setRegistering(!registering)
              setPassword('')
              setAgain('')
              setError('')
            }}
          >
            {registering
              ? '已有账号？返回登录'
              : '还没有教师账号？注册教师账号'}
          </Button>
          <p className="text-muted-foreground text-xs leading-6">
            教师可自行注册。学生请使用教师发放的登录设置链接，登录后仅可查看本人的任务与进度。
          </p>
        </form>
      </section>
    </main>
  )
}
