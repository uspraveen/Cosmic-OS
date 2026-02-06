import LiquidGlass from './LiquidGlass'
import './history.css'

interface Session { id: string; title: string; created_at: number }
interface HistoryPanelProps {
  isOpen: boolean
  sessions: Session[]
  activeSessionId: string | null
  onSelectSession: (id: string) => void
  onClose: () => void
  onNewChat: () => void
}

export default function HistoryPanel({ isOpen, sessions, activeSessionId, onSelectSession, onClose, onNewChat }: HistoryPanelProps) {
  if (!isOpen) return null
  return (
    <div className="history-overlay" onClick={onClose}>
      <div className="history-panel" onClick={e => e.stopPropagation()}>
        <LiquidGlass cornerRadius={24} disableTilt={true}>
          <div className="history-content">
            <div className="history-header">
              <span>Chat History</span>
              <div style={{display:'flex', gap:8}}>
                <button className="new-chat-btn" onClick={onNewChat}>+</button>
                <button className="close-icon-btn" onClick={onClose}>✕</button>
              </div>
            </div>
            <div className="sessions-list">
              {sessions.length === 0 ? <div className="empty-history">No history yet</div> : sessions.map(s => (
                <div key={s.id} className={`session-item ${s.id === activeSessionId ? 'active' : ''}`}
                  onClick={() => { onSelectSession(s.id); onClose(); }}>
                  <div className="session-title">{s.title || "Untitled"}</div>
                  <div className="session-date">{new Date(s.created_at * 1000).toLocaleDateString()}</div>
                </div>
              ))}
            </div>
          </div>
        </LiquidGlass>
      </div>
    </div>
  )
}