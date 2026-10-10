import { useState, useEffect, useCallback } from 'react'

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || 'http://127.0.0.1:8000'

type ConnectionStatus = 'checking' | 'connected' | 'disconnected'

export default function App() {
  const [status, setStatus] = useState<ConnectionStatus>('checking')
  const [errorMessage, setErrorMessage] = useState<string | null>(null)
  const [lastChecked, setLastChecked] = useState<string | null>(null)

  const checkHealth = useCallback(async () => {
    setStatus('checking')
    setErrorMessage(null)
    try {
      const response = await fetch(`${API_BASE_URL}/health`, {
        method: 'GET',
        headers: {
          'Accept': 'application/json',
        },
      })

      if (!response.ok) {
        throw new Error(`Server returned HTTP ${response.status}: ${response.statusText}`)
      }

      const data = await response.json()
      if (data && data.status === 'ok') {
        setStatus('connected')
        setErrorMessage(null)
      } else {
        setStatus('disconnected')
        setErrorMessage(`Unexpected response payload: ${JSON.stringify(data)}`)
      }
    } catch (err: unknown) {
      setStatus('disconnected')
      const msg = err instanceof Error ? err.message : 'Network error or backend unreachable'
      setErrorMessage(msg)
    } finally {
      setLastChecked(new Date().toLocaleTimeString())
    }
  }, [])

  useEffect(() => {
    checkHealth()
  }, [checkHealth])

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100 flex flex-col font-sans selection:bg-cyan-500 selection:text-slate-950">
      {/* Top Navigation Bar */}
      <header className="border-b border-slate-800/80 bg-slate-900/60 backdrop-blur-md sticky top-0 z-10 px-6 py-4 flex items-center justify-between">
        <div className="flex items-center space-x-3">
          <div className="h-9 w-9 rounded-lg bg-gradient-to-tr from-cyan-500 to-blue-600 flex items-center justify-center font-bold text-slate-950 shadow-md shadow-cyan-500/20">
            S
          </div>
          <div>
            <h1 className="text-lg font-semibold tracking-tight text-white">
              SEBI Circular Assistant
            </h1>
            <p className="text-xs text-slate-400">
              Supersession-Aware Regulatory Retrieval (Portfolio Prototype)
            </p>
          </div>
        </div>

        {/* Backend Connection Indicator */}
        <div className="flex items-center space-x-3 bg-slate-900 border border-slate-800 px-3.5 py-1.5 rounded-full text-xs font-medium">
          <span className="text-slate-400">API Status:</span>
          {status === 'checking' && (
            <span className="flex items-center space-x-1.5 text-amber-400">
              <span className="h-2 w-2 rounded-full bg-amber-400 animate-ping"></span>
              <span>Checking...</span>
            </span>
          )}
          {status === 'connected' && (
            <span className="flex items-center space-x-1.5 text-emerald-400">
              <span className="h-2 w-2 rounded-full bg-emerald-400"></span>
              <span>Connected</span>
            </span>
          )}
          {status === 'disconnected' && (
            <span className="flex items-center space-x-1.5 text-rose-400">
              <span className="h-2 w-2 rounded-full bg-rose-400"></span>
              <span>Disconnected</span>
            </span>
          )}
        </div>
      </header>

      {/* Main Container */}
      <main className="flex-1 max-w-4xl w-full mx-auto px-6 py-10 flex flex-col items-center justify-center">
        <div className="w-full bg-slate-900/70 border border-slate-800 rounded-2xl p-8 shadow-xl backdrop-blur-sm">
          <div className="flex items-start justify-between">
            <div>
              <span className="inline-flex items-center px-2.5 py-1 rounded-md text-xs font-medium bg-cyan-950/80 text-cyan-400 border border-cyan-800/60 mb-3">
                Phase 1 • Vertical Slice Foundation
              </span>
              <h2 className="text-2xl font-bold tracking-tight text-white sm:text-3xl">
                SEBI Circular Q&A
              </h2>
              <p className="mt-2 text-sm text-slate-400 max-w-2xl leading-relaxed">
                Exploring supersession-aware retrieval and date-grounded reasoning over official regulatory circulars. This interface connects to the local FastAPI backend service.
              </p>
            </div>
          </div>

          {/* Diagnostic Card */}
          <div className="mt-8 pt-6 border-t border-slate-800/80 grid gap-4 sm:grid-cols-2">
            <div className="p-4 rounded-xl bg-slate-950/60 border border-slate-800">
              <span className="text-xs uppercase tracking-wider text-slate-500 font-semibold block mb-1">
                Backend Endpoint
              </span>
              <code className="text-xs font-mono text-cyan-300 break-all">
                {API_BASE_URL}/health
              </code>
            </div>

            <div className="p-4 rounded-xl bg-slate-950/60 border border-slate-800 flex items-center justify-between">
              <div>
                <span className="text-xs uppercase tracking-wider text-slate-500 font-semibold block mb-1">
                  Connection Health
                </span>
                <span className={`text-sm font-semibold capitalize ${
                  status === 'connected' ? 'text-emerald-400' :
                  status === 'disconnected' ? 'text-rose-400' : 'text-amber-400'
                }`}>
                  {status}
                </span>
              </div>
              <button
                onClick={checkHealth}
                disabled={status === 'checking'}
                className="px-3 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 active:bg-slate-600 disabled:opacity-50 text-xs font-medium text-slate-200 transition-colors border border-slate-700 cursor-pointer"
              >
                {status === 'checking' ? 'Testing...' : 'Retry'}
              </button>
            </div>
          </div>

          {/* Error Message Details */}
          {status === 'disconnected' && (
            <div className="mt-4 p-4 rounded-xl bg-rose-950/40 border border-rose-900/60 text-xs text-rose-300">
              <div className="font-semibold flex items-center space-x-1.5 mb-1">
                <span>Network Diagnostic:</span>
              </div>
              <p className="font-mono text-[11px] opacity-90">{errorMessage || 'Unable to establish HTTP connection.'}</p>
              <p className="mt-2 text-rose-400/80 text-[11px]">
                Ensure the FastAPI backend is running locally at <code className="font-mono">{API_BASE_URL}</code>.
              </p>
            </div>
          )}

          {lastChecked && (
            <p className="mt-4 text-[11px] text-slate-500 text-right">
              Last probe: {lastChecked}
            </p>
          )}
        </div>

        {/* Informational Notice */}
        <div className="mt-6 text-center text-xs text-slate-500 max-w-lg">
          Educational portfolio project. Answers are informational and verified against official circular provenance. Not affiliated with SEBI.
        </div>
      </main>
    </div>
  )
}
