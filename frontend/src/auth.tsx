import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useState,
  type ReactNode,
} from 'react'
import { fetchClient, queryClient } from '@/api/client'
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
  registerTeacher: (username: string, password: string) => Promise<void>
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

  async function authenticate(
    path: '/api/auth/login' | '/api/auth/register',
    username: string,
    password: string,
  ) {
    const { data } = await fetchClient.POST(path, {
      body: { username, password },
    })
    if (!data) throw new Error('未收到登录结果，请重试')
    clear()
    setUser(data)
  }

  async function login(username: string, password: string) {
    await authenticate('/api/auth/login', username, password)
  }

  async function registerTeacher(username: string, password: string) {
    await authenticate('/api/auth/register', username, password)
  }

  async function logout() {
    try {
      await fetch('/api/auth/logout', { method: 'POST' })
    } finally {
      clear()
    }
  }

  return (
    <Context.Provider value={{ user, loading, login, registerTeacher, logout }}>
      {children}
    </Context.Provider>
  )
}

export function useAuth() {
  const value = useContext(Context)
  if (!value) throw new Error('AuthProvider missing')
  return value
}
