/**
 * 设置页「修改密码」。只改当前登录账号，成功后回到登录页。
 */
import { useEffect, useState, type FormEvent } from 'react'
import { useMutation } from '@tanstack/react-query'
import { Eye, EyeOff, Loader2, Lock, ShieldAlert } from 'lucide-react'
import { ApiError, api } from '@/lib/api'
import { replacementPasswordError } from '@/lib/passwordPolicy'

export function SettingsPasswordPanel(_props: { highlight?: string }) {
  const [username, setUsername] = useState<string | null>(null)
  const [ready, setReady] = useState(false)
  const [current, setCurrent] = useState('')
  const [next, setNext] = useState('')
  const [confirm, setConfirm] = useState('')
  const [show, setShow] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    let cancelled = false
    api.authStatus()
      .then(status => {
        if (cancelled) return
        setUsername(status.authenticated ? (status.username ?? '') : null)
      })
      .catch(() => {
        if (!cancelled) setUsername(null)
      })
      .finally(() => {
        if (!cancelled) setReady(true)
      })
    return () => {
      cancelled = true
    }
  }, [])

  const submit = useMutation({
    mutationFn: () => api.authChangePassword(current, next, confirm),
    onSuccess: () => {
      window.location.assign('/login?changed=1')
    },
    onError: (err: unknown) => {
      const message = err instanceof ApiError
        ? err.message
        : err instanceof Error
          ? err.message
          : '修改失败'
      setError(message || '修改失败')
    },
  })

  const loggedIn = username !== null
  const localIssue = (() => {
    if (!current || !next || !confirm) return '请填写当前密码，并把新密码输入两次'
    if (next !== confirm) return '两次输入的新密码不一致'
    return replacementPasswordError(next, current)
  })()

  const handleSubmit = (event: FormEvent) => {
    event.preventDefault()
    setError('')
    if (!loggedIn) {
      setError('请先登录')
      return
    }
    if (localIssue) {
      setError(localIssue)
      return
    }
    submit.mutate()
  }

  return (
    <section className="mx-auto w-full max-w-lg rounded-card border border-border bg-surface p-4 sm:p-5">
      <div className="mb-4 flex items-start gap-2.5">
        <div className="grid h-9 w-9 shrink-0 place-items-center rounded-lg bg-accent/15 text-accent">
          <Lock className="h-4 w-4" />
        </div>
        <div className="min-w-0">
          <h2 className="text-sm font-medium text-foreground">修改密码</h2>
          <p className="mt-0.5 text-[11px] leading-relaxed text-muted">
            {ready
              ? loggedIn
                ? `当前账号 ${username || 'admin'}。只能修改自己的密码。`
                : '请先登录后再修改密码。'
              : '正在确认当前账号…'}
          </p>
        </div>
      </div>

      <form onSubmit={handleSubmit} className="space-y-3">
        <PasswordField
          label="当前密码"
          name="current-password"
          autoComplete="current-password"
          value={current}
          shown={show}
          autoFocus
          disabled={!ready || !loggedIn || submit.isPending}
          onChange={setCurrent}
          onToggle={() => setShow(value => !value)}
        />
        <PasswordField
          label="新密码"
          name="new-password"
          autoComplete="new-password"
          value={next}
          shown={show}
          disabled={!ready || !loggedIn || submit.isPending}
          onChange={setNext}
          onToggle={() => setShow(value => !value)}
        />
        <PasswordField
          label="再输入一次新密码"
          name="confirm-password"
          autoComplete="new-password"
          value={confirm}
          shown={show}
          disabled={!ready || !loggedIn || submit.isPending}
          onChange={setConfirm}
          onToggle={() => setShow(value => !value)}
        />

        <p className="text-[11px] leading-relaxed text-muted">
          新密码至少 8 位，并且包含字母、数字、符号中的至少两类，不能与当前密码相同。
          修改成功后，这个账号在其他设备上的登录会失效，当前浏览器也需要用新密码重新登录。
          当前密码连续填错 5 次，会和登录失败一起锁定 5 分钟。
        </p>

        {error && (
          <div className="flex items-start gap-1.5 rounded-btn bg-danger/10 px-3 py-2 text-[11px] text-danger">
            <ShieldAlert className="mt-px h-3.5 w-3.5 shrink-0" />
            <span className="min-w-0 break-words">{error}</span>
          </div>
        )}

        <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
          <button
            type="submit"
            disabled={!ready || !loggedIn || submit.isPending || !current || !next || !confirm}
            className="inline-flex h-11 w-full items-center justify-center gap-1.5 rounded-btn bg-accent text-sm font-medium text-white transition-colors hover:bg-accent/90 disabled:opacity-50 sm:h-10 sm:w-auto sm:px-4"
          >
            {submit.isPending ? (
              <><Loader2 className="h-4 w-4 animate-spin" />正在保存…</>
            ) : (
              '修改密码'
            )}
          </button>
          {!loggedIn && ready && (
            <a href="/login?redirect=%2Fsettings%3Ftab%3Dpassword" className="text-center text-xs text-accent sm:text-left">
              去登录
            </a>
          )}
        </div>
      </form>
    </section>
  )
}

function PasswordField({
  label,
  name,
  autoComplete,
  value,
  shown,
  autoFocus,
  disabled,
  onChange,
  onToggle,
}: {
  label: string
  name: string
  autoComplete: string
  value: string
  shown: boolean
  autoFocus?: boolean
  disabled?: boolean
  onChange: (value: string) => void
  onToggle: () => void
}) {
  return (
    <label className="block">
      <span className="text-[11px] text-muted">{label}</span>
      <span className="relative mt-1 block">
        <input
          type={shown ? 'text' : 'password'}
          name={name}
          autoComplete={autoComplete}
          autoFocus={autoFocus}
          value={value}
          disabled={disabled}
          onChange={event => onChange(event.target.value)}
          className="h-11 w-full rounded-btn border border-border bg-base px-3 pr-11 text-base text-foreground outline-none transition-colors focus:border-accent/50 disabled:opacity-50 md:h-10 md:text-sm"
        />
        <button
          type="button"
          onClick={onToggle}
          className="absolute right-1 top-1/2 grid h-9 w-9 -translate-y-1/2 place-items-center text-muted hover:text-foreground"
          tabIndex={-1}
          aria-label={shown ? '隐藏密码' : '显示密码'}
        >
          {shown ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
        </button>
      </span>
    </label>
  )
}
