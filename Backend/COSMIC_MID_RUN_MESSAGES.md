# Mid-run messages (steer / queue)

The composer stays open while Cosmic works. A message sent during a run is
either **steered** into the running agent at its next safe step, or
**queued** to run as its own turn once the current one finishes. This mirrors
how Codex (Enter = steer, Tab = queue; undeliverable steers fall back to the
end of the turn) and Claude Code (queued messages run next; Esc interrupts
and the queue is kept) handle input mid-run.

## Keys and UI (desktop)

| While Cosmic is working | Effect |
| --- | --- |
| Enter / send arrow | Steer: Cosmic reads it at its next step |
| Tab | Queue: runs as its own turn afterwards |
| Stop | Cancels the run; held messages are kept and sent next |
| Pencil on a tray item | Withdraws it and puts the text back in the composer |
| X on a tray item | Withdraws it |

The tray above the input shows each held message as Sending, Waiting
(pending steer), Reading (being injected), Queued, Starting, or Failed.
A delivered steer shows up in the chat with a "Read mid-run" caption.
Esc was left alone because it already hides the window.

## Flow

```
desktop/mobile WS "query" {delivery: steer|queue}
  -> gateway MidRunCoordinator.accept()
       idle channel            -> run_realtime_query()   (unchanged path)
       busy / dispatching /    -> HeldMessageQueue (per channel, max 50)
       already holding            user_message.queued + user_message.queue
  -> pump:
       foreground orchestrator run with task_id, steer item
         -> POST /internal/tasks/{task_id}/messages   (SteerInbox.submit)
       no foreground run -> dispatch waiting[0] as a normal turn
                            (user_message.dispatched)
orchestrator tool loop, top of each iteration (last message is user/tool):
  SteerInbox.drain() -> appended as a user nudge -> task.user_message_delivered
  inbox closed when the loop exits, BEFORE any terminal event
gateway on task.user_message_delivered:
  persist user message (metadata.delivery = "mid_run_steer") -> user_message.delivered
gateway on request finalize:
  items accepted by the inbox but never drained go back to waiting -> next turn
```

## Why it is exactly once

* The orchestrator inbox is closed before any terminal event, and deliveries
  are on the same ordered stream as that terminal event. So when the gateway
  sees the run finish, every drained message has already been reported.
* Anything still "injecting" for that run at finalize was never drained, so
  it is released back to waiting and dispatched as a follow-up turn.
* Submission is idempotent per message id. Withdraw is atomic in the inbox
  and still works after close. Every state change after an await is re-checked.
* History reads: user A, user B (steer), assistant A, because B is persisted
  at the moment the model sees it.

## Limits

* Steers with attachments become follow-up turns. Editing restores text only.
* Direct-model routes (no orchestrator tool loop) cannot be steered, so those
  messages wait for the next turn.
* When a run is backgrounded, its waiting steers dispatch as a new turn.
* WhatsApp / Telegram / email are unchanged.
* Mobile uses the same WS path, so busy queries are now held instead of
  rejected. The mobile UI has no tray yet.

## Background / foreground (BG/FG)

Before this change, backgrounding a run was the only way to talk to Cosmic
while it worked. Now that the composer never locks, that job is gone.
BG/FG is still useful for real parallel work (several long tasks per
session, autonomous runs), so it is kept unchanged here. See the PR for the
keep / simplify / remove assessment.
