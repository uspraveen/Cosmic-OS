import { useEffect, useState } from 'react'
import './monitor-selector.css'

interface DisplayInfo {
    id: number
    label: string
    bounds: { x: number; y: number; width: number; height: number }
    workArea: { x: number; y: number; width: number; height: number }
    scaleFactor: number
    rotation: number
    isPrimary: boolean
    isPreferred: boolean
}

export default function MonitorSelector() {
    const [displays, setDisplays] = useState<DisplayInfo[]>([])
    const [selectedId, setSelectedId] = useState<number | null>(null)

    useEffect(() => {
        loadDisplays()

        // Listen for display preference updates
        const handler = (newId: number) => {
            setSelectedId(newId)
        }
            ; (window as any).ipcRenderer.on('display-preferences-updated', handler)

        return () => {
            ; (window as any).ipcRenderer.off('display-preferences-updated', handler)
        }
    }, [])

    const loadDisplays = async () => {
        try {
            const list = await (window as any).ipcRenderer.invoke('get-all-displays')
            setDisplays(list)
            const preferred = list.find((d: DisplayInfo) => d.isPreferred)
            if (preferred) setSelectedId(preferred.id)
            else if (list.length > 0) setSelectedId(list[0].id)
        } catch (error) {
            console.error('Failed to load displays:', error)
        }
    }

    const selectDisplay = (id: number) => {
        setSelectedId(id)
            ; (window as any).ipcRenderer.send('set-preferred-display', id)
    }

    if (displays.length === 0) {
        return (
            <div className="monitor-selector-empty">
                <span>Loading displays...</span>
            </div>
        )
    }

    return (
        <div className="monitor-selector">
            {displays.map(display => (
                <MonitorCard
                    key={display.id}
                    display={display}
                    isSelected={selectedId === display.id}
                    onSelect={() => selectDisplay(display.id)}
                />
            ))}
            <button className="refresh-displays-btn" onClick={loadDisplays}>
                <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor">
                    <path d="M17.65 6.35A7.958 7.958 0 0 0 12 4c-4.42 0-7.99 3.58-7.99 8s3.57 8 7.99 8c3.73 0 6.84-2.55 7.73-6h-2.08A5.99 5.99 0 0 1 12 18c-3.31 0-6-2.69-6-6s2.69-6 6-6c1.66 0 3.14.69 4.22 1.78L13 11h7V4l-2.35 2.35z" />
                </svg>
                Refresh Displays
            </button>
        </div>
    )
}

interface MonitorCardProps {
    display: DisplayInfo
    isSelected: boolean
    onSelect: () => void
}

function MonitorCard({ display, isSelected, onSelect }: MonitorCardProps) {
    return (
        <div
            className={`monitor-card ${isSelected ? 'selected' : ''}`}
            onMouseDown={onSelect}
        >
            <div className="monitor-viz">
                <svg width="64" height="50" viewBox="0 0 64 50">
                    {/* Monitor bezel */}
                    <rect
                        x="2" y="2" width="60" height="42"
                        fill="rgba(65, 65, 70, 0.8)"
                        stroke="rgba(110, 110, 115, 1)"
                        strokeWidth="1.5"
                        rx="4"
                    />
                    {/* Screen */}
                    <rect
                        x="4" y="4" width="56" height="38"
                        fill="url(#screenGradient)"
                        stroke="rgba(90, 90, 95, 1)"
                        strokeWidth="1"
                        rx="2"
                    />
                    {/* Primary indicator */}
                    {display.isPrimary && (
                        <circle cx="54" cy="10" r="4" fill="#FFC107" />
                    )}
                    <defs>
                        <linearGradient id="screenGradient" x1="0%" y1="0%" x2="0%" y2="100%">
                            <stop offset="0%" stopColor="rgba(35, 35, 42, 1)" />
                            <stop offset="100%" stopColor="rgba(20, 20, 26, 1)" />
                        </linearGradient>
                    </defs>
                </svg>
            </div>

            <div className="monitor-info">
                <div className="monitor-name">{display.label}</div>
                <div className="monitor-res">
                    {display.bounds.width} × {display.bounds.height}
                    {display.scaleFactor !== 1 && ` (@${display.scaleFactor}x)`}
                </div>
                <div className="monitor-badges">
                    {display.isPrimary && (
                        <span className="badge primary">PRIMARY</span>
                    )}
                    {isSelected && (
                        <span className="badge selected">SELECTED</span>
                    )}
                </div>
            </div>
        </div>
    )
}
