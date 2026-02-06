import React, { useMemo } from 'react'

interface CalendarMonthViewProps {
  currentDate: Date
  events: any[]
}

export default function CalendarMonthView({ currentDate, events }: CalendarMonthViewProps) {
  const { days, monthLabel } = useMemo(() => {
    const year = currentDate.getFullYear()
    const month = currentDate.getMonth()
    
    const firstDay = new Date(year, month, 1)
    const lastDay = new Date(year, month + 1, 0)
    
    const daysArr = []
    // Padding
    for (let i = 0; i < firstDay.getDay(); i++) daysArr.push(null)
    // Days
    for (let i = 1; i <= lastDay.getDate(); i++) daysArr.push(new Date(year, month, i))

    return {
      days: daysArr,
      monthLabel: firstDay.toLocaleString('default', { month: 'long', year: 'numeric' })
    }
  }, [currentDate])

  // FIX: Safe Local Date Comparison
  const hasEvent = (day: Date | null) => {
    if (!day) return false
    
    // Create a local YYYY-MM-DD string for the calendar cell
    const cellDateStr = `${day.getFullYear()}-${String(day.getMonth()+1).padStart(2,'0')}-${String(day.getDate()).padStart(2,'0')}`
    
    return events.some(e => {
        // The python script now sends ISO strings with offsets (e.g. 2026-01-25T18:00:00-06:00)
        // We act like a "Day View" and just check if the YYYY-MM-DD matches
        return e.start.startsWith(cellDateStr)
    })
  }
  
  const isToday = (day: Date | null) => {
      if (!day) return false
      const now = new Date()
      return day.getDate() === now.getDate() && 
             day.getMonth() === now.getMonth() && 
             day.getFullYear() === now.getFullYear()
  }

  return (
    <div style={{ width: '100%', height: '100%', padding: '20px', display: 'flex', flexDirection: 'column' }}>
      <div style={{ fontSize: '18px', fontWeight: '600', color: '#fff', marginBottom: '16px' }}>
        {monthLabel}
      </div>
      
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(7, 1fr)', gap: '8px', textAlign: 'center' }}>
        {['S','M','T','W','T','F','S'].map(d => (
          <div key={d} style={{ fontSize: '11px', color: 'rgba(255,255,255,0.4)', fontWeight:'700' }}>{d}</div>
        ))}
        
        {days.map((day, i) => (
          <div key={i} style={{ 
            height: '32px', display: 'flex', alignItems: 'center', justifyContent: 'center',
            fontSize: '13px', color: day ? '#fff' : 'transparent',
            background: hasEvent(day) ? 'rgba(0, 122, 255, 0.2)' : 'transparent',
            borderRadius: '8px', position: 'relative',
            border: isToday(day) ? '1px solid #007AFF' : 'none',
            cursor: day ? 'default' : 'none'
          }}>
            {day ? day.getDate() : ''}
            {hasEvent(day) && (
              <div style={{ position: 'absolute', bottom: '4px', width: '4px', height: '4px', background: '#007AFF', borderRadius:'50%' }} />
            )}
          </div>
        ))}
      </div>
    </div>
  )
}