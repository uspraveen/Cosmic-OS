import { useEffect, useMemo, useState } from 'react'
import {
  formatCalendarRange,
  getCalendarEventStart,
  isCalendarEventOnDate,
  isSameCalendarDay,
  type CalendarAgendaAccount,
  type CalendarAgendaEvent,
} from './calendar'

interface CalendarMonthViewProps {
  currentDate: Date
  events: CalendarAgendaEvent[]
  accounts: CalendarAgendaAccount[]
  onEventSelect: (event: CalendarAgendaEvent) => void
}

const WEEKDAY_LABELS = ['S', 'M', 'T', 'W', 'T', 'F', 'S']

function getDayKey(date: Date) {
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`
}

export default function CalendarMonthView({ currentDate, events, accounts, onEventSelect }: CalendarMonthViewProps) {
  const [selectedDate, setSelectedDate] = useState(() => new Date(currentDate.getFullYear(), currentDate.getMonth(), currentDate.getDate()))
  const currentYear = currentDate.getFullYear()
  const currentMonth = currentDate.getMonth()
  const currentDay = currentDate.getDate()

  useEffect(() => {
    setSelectedDate(new Date(currentYear, currentMonth, currentDay))
  }, [currentDay, currentMonth, currentYear])

  const { days, monthLabel, eventCountByDay } = useMemo(() => {
    const year = currentDate.getFullYear()
    const month = currentDate.getMonth()
    const firstDay = new Date(year, month, 1)
    const lastDay = new Date(year, month + 1, 0)
    const daysArr: Array<Date | null> = []
    const counts = new Map<string, number>()

    for (let i = 0; i < firstDay.getDay(); i += 1) {
      daysArr.push(null)
    }
    for (let day = 1; day <= lastDay.getDate(); day += 1) {
      daysArr.push(new Date(year, month, day))
    }

    for (const event of events) {
      const date = getCalendarEventStart(event)
      if (!date) continue
      const key = getDayKey(date)
      counts.set(key, (counts.get(key) ?? 0) + 1)
    }

    return {
      days: daysArr,
      monthLabel: firstDay.toLocaleString([], { month: 'long', year: 'numeric' }),
      eventCountByDay: counts,
    }
  }, [currentDate, events])

  const selectedEvents = useMemo(
    () => events.filter((event) => isCalendarEventOnDate(event, selectedDate)).slice(0, 3),
    [events, selectedDate],
  )

  const connectedAccounts = accounts.filter((account) => account.tool_enabled && !account.needs_reconnect).length
  const selectedLabel = selectedDate.toLocaleDateString([], { weekday: 'long', month: 'short', day: 'numeric' })
  const today = new Date()

  return (
    <div className="month-view">
      <div className="month-view-header">
        <div>
          <span className="month-view-kicker">Calendar</span>
          <h3>{monthLabel}</h3>
          <p>{connectedAccounts === 0 ? 'No synced accounts' : `${connectedAccounts} synced account${connectedAccounts === 1 ? '' : 's'}`}</p>
        </div>
        <div className="month-view-summary">
          <strong>{events.length}</strong>
          <span>{events.length === 1 ? 'event' : 'events'}</span>
        </div>
      </div>

      <div className="month-view-weekdays">
        {WEEKDAY_LABELS.map((label, i) => (
          <span key={`${label}-${i}`}>{label}</span>
        ))}
      </div>

      <div className="month-view-grid">
        {days.map((day, index) => {
          if (!day) {
            return <div key={`empty-${index}`} className="month-view-day month-view-day-empty" aria-hidden="true" />
          }

          const key = getDayKey(day)
          const count = eventCountByDay.get(key) ?? 0
          const selected = isSameCalendarDay(day, selectedDate)
          const isToday = isSameCalendarDay(day, today)

          return (
            <button
              key={key}
              type="button"
              className={`month-view-day ${selected ? 'selected' : ''} ${count > 0 ? 'has-events' : ''} ${isToday ? 'today' : ''}`}
              onClick={() => setSelectedDate(day)}
            >
              <span className="month-view-day-number">{day.getDate()}</span>
              {count > 0 && (
                <span className="month-view-day-meta">
                  <span className="month-view-day-dot" />
                  {count > 1 && <span className="month-view-day-count">{count}</span>}
                </span>
              )}
            </button>
          )
        })}
      </div>

      <div className="month-view-agenda">
        <div className="month-view-agenda-head">
          <div>
            <span className="month-view-agenda-label">{selectedLabel}</span>
            <strong>{selectedEvents.length === 0 ? 'No events' : `${selectedEvents.length} event${selectedEvents.length === 1 ? '' : 's'}`}</strong>
          </div>
          <span className="month-view-agenda-meta">{connectedAccounts === 0 ? 'Connect in Settings' : 'Select a day'}</span>
        </div>

        {selectedEvents.length > 0 ? (
          <div className="month-view-agenda-list">
            {selectedEvents.map((event) => (
              <button
                key={`${event.account_id}-${event.id}-${event.start}`}
                type="button"
                className="month-view-agenda-item"
                onClick={() => onEventSelect(event)}
              >
                <div className="month-view-agenda-copy">
                  <span className="month-view-agenda-time">{formatCalendarRange(event)}</span>
                  <strong>{event.summary}</strong>
                  <span>{event.calendar_name}{event.location ? ` \u00B7 ${event.location}` : ''}</span>
                </div>
                <span className="month-view-agenda-account">{event.account_label}</span>
              </button>
            ))}
          </div>
        ) : (
          <div className="month-view-empty-state">
            <span>Nothing scheduled</span>
          </div>
        )}
      </div>
    </div>
  )
}
