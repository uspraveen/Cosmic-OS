import { useEffect, useRef, useState } from 'react'
import {
  customRow,
  normalizeQuestionRows,
  questionCustomAllowed,
  questionKeyRow,
  resolveQuestionAnswer,
  type QuestionSelection,
} from './questionCardLogic'

export interface QuestionCardProps {
  question: string
  options: string[]
  allowCustom?: boolean
  context?: string | null
  /** Parent-rendered status chip in the head row, e.g. "1 of 2 waiting". */
  counterLabel?: string | null
  busy?: boolean
  error?: string | null
  sent?: boolean
  onContinue: (answer: string) => void
  onSkip?: () => void
}

const isTypingTarget = (target: EventTarget | null) => {
  if (!(target instanceof HTMLElement)) return false
  if (target.isContentEditable) return true
  const tag = target.tagName
  return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT'
}

export function QuestionCard({
  question,
  options,
  allowCustom = true,
  context,
  counterLabel,
  busy = false,
  error,
  sent = false,
  onContinue,
  onSkip,
}: QuestionCardProps) {
  const rowsInfo = normalizeQuestionRows(options)
  const customAllowed = questionCustomAllowed(rowsInfo, allowCustom)
  const custom = customRow(rowsInfo.rows.length)
  // An open question (no usable options) starts on the custom row; a choosable
  // one starts with nothing picked so Continue can't silently fire.
  const initialSelection: QuestionSelection = rowsInfo.rows.length === 0 && customAllowed ? custom : null

  const [selected, setSelected] = useState<QuestionSelection>(initialSelection)
  const [customValue, setCustomValue] = useState('')
  const [localError, setLocalError] = useState('')
  const customInputRef = useRef<HTMLInputElement | null>(null)

  useEffect(() => {
    setSelected(initialSelection)
    setCustomValue('')
    setLocalError('')
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [question, options])

  useEffect(() => {
    if (selected === custom) customInputRef.current?.focus()
  }, [selected, custom])

  const resolvedError = error || localError

  const selectRow = (row: number) => {
    if (busy || sent) return
    setLocalError('')
    setSelected(row)
  }

  const handleContinue = () => {
    if (busy || sent) return
    const result = resolveQuestionAnswer({
      rowsInfo,
      customAllowed,
      selected,
      customValue,
    })
    if (!result.ok) {
      setLocalError(result.error)
      return
    }
    onContinue(result.answer)
  }

  // Letter and number keys select a row without needing focus on the card —
  // but never while the user is typing somewhere else (the composer, search).
  useEffect(() => {
    if (sent || busy) return undefined
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.metaKey || event.ctrlKey || event.altKey) return
      if (isTypingTarget(event.target)) return
      const row = questionKeyRow(event.key, rowsInfo.rows.length, customAllowed)
      if (row !== null) {
        event.preventDefault()
        selectRow(row)
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rowsInfo.rows.length, customAllowed, sent, busy])

  const onContainerKeyDown = (event: React.KeyboardEvent) => {
    if (event.key === 'Escape') {
      if (selected === custom && document.activeElement === customInputRef.current) {
        customInputRef.current?.blur()
        return
      }
      if (onSkip) {
        event.stopPropagation()
        onSkip()
      }
      return
    }
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault()
      handleContinue()
    }
  }

  return (
    <section
      className={`ask-card${sent ? ' is-sent' : ''}`}
      data-kind="ask_user_question"
      aria-label={question}
      onKeyDown={onContainerKeyDown}
    >
      <div className="ask-card-head">
        <div className="ask-card-kicker-cluster">
          <span className="ask-card-kicker" aria-hidden="true">
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none">
              <path
                d="M12 17v.5M9.1 9a3 3 0 1 1 5.82 1c-.5 1.7-2.42 2.1-2.92 3.5"
                stroke="currentColor"
                strokeWidth="1.8"
                strokeLinecap="round"
              />
              <circle cx="12" cy="12" r="9.2" stroke="currentColor" strokeWidth="1.6" />
            </svg>
            Cosmic asks
          </span>
          {counterLabel && <span className="ask-card-counter">{counterLabel}</span>}
        </div>
      </div>
      <div className="ask-card-question">{question}</div>
      {context && <div className="ask-card-context">{context}</div>}
      <div className="ask-card-options" role="listbox" aria-label="Answer choices">
        {rowsInfo.rows.map((option, index) => {
          const isSelected = selected === index
          return (
            <button
              key={`${index}-${option}`}
              type="button"
              role="option"
              aria-selected={isSelected}
              className={`ask-card-option${isSelected ? ' is-selected' : ''}`}
              disabled={busy || sent}
              onClick={() => selectRow(index)}
            >
              <span className="ask-card-option-key" aria-hidden="true">
                {String.fromCharCode(65 + index)}
              </span>
              <span className="ask-card-option-label">{option}</span>
            </button>
          )
        })}
        {customAllowed && (
          <button
            type="button"
            role="option"
            aria-selected={selected === custom}
            className={`ask-card-option is-custom${selected === custom ? ' is-selected' : ''}`}
            disabled={busy || sent}
            onClick={() => selectRow(custom)}
          >
            <span className="ask-card-option-key" aria-hidden="true">
              {String.fromCharCode(65 + rowsInfo.rows.length)}
            </span>
            <span className="ask-card-option-label">Custom answer…</span>
          </button>
        )}
      </div>
      {customAllowed && selected === custom && (
        <input
          ref={customInputRef}
          className="ask-card-custom-input"
          type="text"
          value={customValue}
          autoComplete="off"
          placeholder="Type your answer…"
          disabled={busy || sent}
          spellCheck={false}
          maxLength={2000}
          aria-label="Custom answer"
          onChange={(event) => {
            setCustomValue(event.target.value)
            if (localError) setLocalError('')
          }}
          onKeyDown={(event) => {
            if (event.key === 'Enter' && !event.shiftKey) {
              event.preventDefault()
              event.stopPropagation()
              const result = resolveQuestionAnswer({
                rowsInfo,
                customAllowed,
                selected,
                customValue: event.currentTarget.value,
              })
              if (result.ok) onContinue(result.answer)
              else setLocalError(result.error)
            }
            if (event.key === 'Escape') {
              event.stopPropagation()
              customInputRef.current?.blur()
            }
          }}
        />
      )}
      {resolvedError && <div className="ask-card-error" role="alert">{resolvedError}</div>}
      <div className="ask-card-footer">
        {sent ? (
          <div className="ask-card-sent">Answer sent ✓</div>
        ) : (
          <div className="ask-card-footer-spacer" />
        )}
        {!sent && onSkip && (
          <button
            type="button"
            className="ask-card-skip"
            disabled={busy}
            onClick={() => onSkip()}
          >
            Skip
          </button>
        )}
        {!sent && (
          <button
            type="button"
            className="ask-card-continue"
            disabled={busy}
            onClick={handleContinue}
          >
            {busy ? 'Sending…' : 'Continue'}
          </button>
        )}
      </div>
    </section>
  )
}
