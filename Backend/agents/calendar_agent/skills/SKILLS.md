# Calendar Agent — Skills

## Google Calendar API Reference

### Endpoints Used
- `GET /calendar/v3/users/me/calendarList` — list calendars
- `GET /calendar/v3/calendars/{calendarId}/events` — list events
- `POST /calendar/v3/calendars/{calendarId}/events` — create event
- `PATCH /calendar/v3/calendars/{calendarId}/events/{eventId}` — update event
- `DELETE /calendar/v3/calendars/{calendarId}/events/{eventId}` — delete event
- `POST /calendar/v3/freeBusy/query` — query free/busy

### Event Object Shape
```json
{
  "event_id": "abc123",
  "calendar_id": "primary",
  "summary": "Meeting with Alex",
  "description": "Discuss Q2 roadmap",
  "location": "Conference Room A",
  "start": "2026-03-30T14:00:00+05:30",
  "end": "2026-03-30T15:00:00+05:30",
  "is_all_day": false,
  "status": "confirmed",
  "attendees": [
    {"email": "alex@example.com", "display_name": "Alex", "response_status": "needsAction"}
  ],
  "recurring_event_id": null
}
```

## Scheduling Patterns

### Duration Inference
- "quick call" → 15 min
- "lunch" → 60 min
- "standup" → 15 min
- "1:1" / "one-on-one" → 30 min
- "interview" → 45-60 min
- "meeting" (unspecified) → 30 min

### Conflict Resolution
- When conflicts exist, warn the user with the conflicting event titles
- Never silently double-book
- If the user insists, create anyway but include the warning

### Working Hours Default
- Monday-Friday, 9:00 AM - 5:00 PM (configurable via env)
- Buffer between meetings: 15 min (configurable)
