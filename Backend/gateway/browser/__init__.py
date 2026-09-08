"""Browser agent mid-run interaction bridge.

Routes for the browser agent's AskUser interrupt — the desktop-facing half of
`Backend/gateway/browser_interrupts.py`. Live view + progress travel over the
existing task.progress -> browser_progress channel (see
`GatewayRuntime._hydrate_browser_progress`); this package is only the
blocking ask/answer round trip.
"""
