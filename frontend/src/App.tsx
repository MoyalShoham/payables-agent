import { useEffect, useState } from 'react'
import { api, ApiError } from './api'
import { DraftView } from './components/DraftView'
import { Audit, Payments, Queue, Upload, Vendors } from './components/Pages'
import type { User } from './types'

type Page =
  | { name: 'queue' }
  | { name: 'draft'; id: number }
  | { name: 'upload' }
  | { name: 'vendors' }
  | { name: 'payments' }
  | { name: 'audit' }

export default function App() {
  const [user, setUser] = useState<User | null>(null)
  const [checked, setChecked] = useState(false)
  const [page, setPage] = useState<Page>({ name: 'queue' })

  useEffect(() => {
    api
      .me()
      .then(setUser)
      .catch(() => setUser(null))
      .finally(() => setChecked(true))
  }, [])

  if (!checked) return null
  if (!user) return <Login onSignedIn={setUser} />

  const tabs: [Page['name'], string][] = [
    ['queue', 'Review queue'],
    ['upload', 'Upload'],
    ['vendors', 'Vendors'],
    ['payments', 'Payments'],
    ['audit', 'Audit log'],
  ]
  return (
    <div className="app">
      <header className="topbar">
        <strong>payables-agent</strong>
        <nav>
          {tabs.map(([name, label]) => (
            <button
              key={name}
              type="button"
              aria-current={page.name === name}
              onClick={() => setPage({ name } as Page)}
            >
              {label}
            </button>
          ))}
        </nav>
        <span className="who">
          {user.display_name} ·{' '}
          <button
            type="button"
            className="link"
            onClick={() => void api.logout().then(() => setUser(null))}
          >
            Sign out
          </button>
        </span>
      </header>
      <main>
        {page.name === 'queue' && <Queue onOpen={(id) => setPage({ name: 'draft', id })} />}
        {page.name === 'draft' && (
          <DraftView id={page.id} user={user} onBack={() => setPage({ name: 'queue' })} />
        )}
        {page.name === 'upload' && <Upload onUploaded={(id) => setPage({ name: 'draft', id })} />}
        {page.name === 'vendors' && <Vendors />}
        {page.name === 'payments' && <Payments />}
        {page.name === 'audit' && <Audit />}
      </main>
      <footer className="muted">Payments are simulated. The agent prepares; people decide.</footer>
    </div>
  )
}

function Login({ onSignedIn }: { onSignedIn: (u: User) => void }) {
  const [username, setUsername] = useState('approver')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  return (
    <form
      className="login card"
      onSubmit={(e) => {
        e.preventDefault()
        api
          .login(username, password)
          .then(onSignedIn)
          .catch((err: unknown) =>
            setError(err instanceof ApiError ? err.message : 'Sign-in failed'),
          )
      }}
    >
      <h1>payables-agent</h1>
      <label className="field">
        <span>Username</span>
        <input
          value={username}
          autoComplete="username"
          onChange={(e) => setUsername(e.target.value)}
        />
      </label>
      <label className="field">
        <span>Password</span>
        <input
          type="password"
          value={password}
          autoComplete="current-password"
          onChange={(e) => setPassword(e.target.value)}
        />
      </label>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      <button type="submit" className="primary">
        Sign in
      </button>
    </form>
  )
}
