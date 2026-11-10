import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useState,
  type ReactNode,
} from 'react'
import { queryClient } from '@/api/client'
import { eventStream } from '@/api/stream'

export type AuthUser = {
  username: string
  role: 'teacher' | 'student'
  student_id: string | null
}
type Auth = {
  user: AuthUser | null
  loading: boolean
  login: (username: string, password: string) => Promise<void>
  logout: () => Promise<void>
}

const Context = createContext<Auth | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<AuthUser | null>(null)
  const [loading, setLoading] = useState(true)
  const clear = useCallback(() => {
    eventStream.reset()
    queryClient.clear()
    setUser(null)
  }, [])

  useEffect(() => {
    let active = true
    fetch('/api/auth/me')
      .then(async (response) => {
        if (active && response.ok) setUser((await response.json()) as AuthUser)
      })
      .catch(() => {})
      .finally(() => {
        if (active) setLoading(false)
      })
    const expired = () => clear()
    window.addEventListener('auth-expired', expired)
    return () => {
      active = false
      window.removeEventListener('auth-expired', expired)
    }
  }, [clear])

  async function login(username: string, password: string) {
    const response = await fetch('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password }),
    })
    if (!response.ok) throw new Error('账号或密码错误')
    const next = (await response.json()) as AuthUser
    clear()
    setUser(next)
  }

  async function logout() {
    try {
      await fetch('/api/auth/logout', { method: 'POST' })
    } finally {
      clear()
    }
  }

  return (
    <Context.Provider value={{ user, loading, login, logout }}>
      {children}
    </Context.Provider>
  )
}

export function useAuth() {
  const value = useContext(Context)
  if (!value) throw new Error('AuthProvider missing')
  return value
}
