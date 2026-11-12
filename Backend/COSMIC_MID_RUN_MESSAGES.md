# Mid-run messages

The composer stays open while Cosmic works. A message sent during a run goes
into the chat straight away as a normal user bubble. Until the agent takes it
in, the bubble is slightly dimmed with a small spinner next to it. There are no
"queued" or "waiting" labels. Then one of two things happens:

* **The running task reads it at its next step.** The spinner goes away and
  the bubble sits inside the reply, at the point where the model read it. The
  text after it is what the model wrote after reading it. The gateway stamps
  that point (the visible stream length at delivery) and stores it with the
  reply, so a reload shows the same placement.
* **It could not be read in time** (the run was on its last step, the route
  cannot be steered, or the user pressed Stop). It runs as its own turn right
  after. The spinner goes and it is an ordinary query with its own answer.

This is how Codex handles input mid-run (steer at the next tool boundary, and
fall back to the end of the turn), shown the way Grok Bot shows it, inline in
the chat.

## Desktop UI

| While Cosmic is working | Effect |
| --- | --- |
| Enter / send arrow | Bubble appears in the chat; Cosmic reads it at its next step |
| Hover the spinner, click | Cancel a message Cosmic has not read yet |
| Retry (only on failure) | Resend a message that did not reach the VM |
| Stop | Cancels the run; unread messages are kept and run next |

* **No Tab-to-queue.** A steer that cannot be read already becomes the next
  turn, so a second gesture adds little, and Tab is normal focus navigation.
  The gateway still accepts `delivery: "queue"` from other clients.
* **No edit.** Cancelling on hover, then retyping, covers it.
* **Esc** was left alone because it already hides the window.

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
  stamp anchor {request_id, content, stream_offset = len(partial_content), delivered_at}
  persist user message (metadata.delivery = "mid_run_steer")
  -> user_message.delivered (+ stream_offset)
  anchors ride response.complete, resume snapshots, stopped/failed turns, and
  the stored reply (metadata.steer_anchors)
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
* Session history (the model's context) reads: user A, user B (steer),
  assistant A, because B is persisted at the moment the model sees it. The
  desktop shows B once, inline in A's reply at its anchor, and drops the
  duplicate user row.

## Limits

* Steers with attachments become follow-up turns.
* Anchors are stream offsets, like the existing card anchors. If the final
  answer text differs from the streamed text (post-processing), the bubble
  can land a few characters off, and it snaps to a sentence boundary.
* Direct-model routes (no orchestrator tool loop) cannot be steered, so those
  messages wait for the next turn.
* When a run is backgrounded, its waiting steers dispatch as a new turn.
* WhatsApp / Telegram / email are unchanged.
* Mobile uses the same WS path, so busy queries are now held instead of
  rejected. The mobile client is not in this repo and has not been updated.

## Background / foreground (BG/FG)

Before this change, backgrounding a run was the only way to talk to Cosmic
while it worked. Now that the composer never locks, that job is gone.
BG/FG is still useful for real parallel work (several long tasks per
session, autonomous runs), so it is kept unchanged here. See the PR for the
keep / simplify / remove assessment.
