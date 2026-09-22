import { useEffect, useRef, useState } from 'react'
import {
  customRow,
  normalizeQuestionFields,
  normalizeQuestionRows,
  questionCustomAllowed,
  questionKeyRow,
  resolveQuestionAnswer,
  resolveRowsAnswers,
  splitOptionLabel,
  type QuestionSelection,
} from './questionCardLogic'

export interface QuestionCardProps {
  question: string
  options: string[]
  /**
   * Form-mode rows; when present the card renders one input per row. Each row
   * picks its own kind: text (free line), single (pick one of its options), or
   * multi (check any) — one card can mix the three.
   */
  fields?: Array<{
    label: string
    placeholder?: string | null
    kind?: string | null
    options?: string[] | null
  }> | null
  allowCustom?: boolean
  context?: string | null
  /** Parent-rendered status chip in the head row, e.g. "1 of 2 waiting". */
  counterLabel?: string | null
  busy?: boolean
  error?: string | null
  sent?: boolean
  /** DOM id so the bottom-right waiting beacon can scroll here and focus it. */
  cardId?: string | null
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
  fields,
  allowCustom = true,
  context,
  counterLabel,
  busy = false,
  error,
  sent = false,
  cardId,
  onContinue,
  onSkip,
}: QuestionCardProps) {
  const rowsInfo = normalizeQuestionRows(options)
  const formFields = normalizeQuestionFields(fields)
  const isForm = formFields.length > 0
  const customAllowed = !isForm && questionCustomAllowed(rowsInfo, allowCustom)
  const custom = customRow(rowsInfo.rows.length)
  // An open question (no usable options) starts on the custom row; a choosable
  // one starts with nothing picked so Continue can't silently fire.
  const initialSelection: QuestionSelection = rowsInfo.rows.length === 0 && customAllowed ? custom : null

  const [selected, setSelected] = useState<QuestionSelection>(initialSelection)
  const [customValue, setCustomValue] = useState('')
  const [formTexts, setFormTexts] = useState<Record<number, string>>({})
  const [formPicks, setFormPicks] = useState<Record<number, number[]>>({})
  const [localError, setLocalError] = useState('')
  const customInputRef = useRef<HTMLInputElement | null>(null)

  // Reset on real content changes only. Keying on the options/fields arrays
  // themselves reset mid-interaction on every replayed task.input_required
  // (a reconnect re-delivers the same ask as fresh arrays) — wiping the
  // selection the user just made and every keystroke they just typed.
  const optionsKey = rowsInfo.rows.join('\u0000')
  const fieldsKey = formFields
    .map((field) => `${field.label}\u0000${field.placeholder || ''}\u0000${field.kind}\u0000${field.options.join(',')}`)
    .join('\u0001')
  useEffect(() => {
    setSelected(initialSelection)
    setCustomValue('')
    setFormTexts({})
    setFormPicks({})
    setLocalError('')
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [question, optionsKey, fieldsKey])

  useEffect(() => {
    if (selected === custom) customInputRef.current?.focus()
  }, [selected, custom])

  const resolvedError = error || localError

  const selectRow = (row: number) => {
    if (busy || sent) return
    setLocalError('')
    setSelected(row)
  }

  const setTextRow = (row: number, value: string) => {
    setFormTexts((prev) => ({ ...prev, [row]: value }))
    if (localError) setLocalError('')
  }

  const selectChoiceRow = (row: number, option: number) => {
    if (busy || sent) return
    setFormPicks((prev) => ({ ...prev, [row]: [option] }))
    if (localError) setLocalError('')
  }

  const toggleMultiRow = (row: number, option: number) => {
    if (busy || sent) return
    setFormPicks((prev) => {
      const current = prev[row] || []
      const next = current.includes(option)
        ? current.filter((item) => item !== option)
        : [...current, option].sort((left, right) => left - right)
      return { ...prev, [row]: next }
    })
    if (localError) setLocalError('')
  }

  const handleContinue = () => {
    if (busy || sent) return
    const result = isForm
      ? resolveRowsAnswers(formFields, { texts: formTexts, picks: formPicks })
      : resolveQuestionAnswer({
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
  // but never in form mode (the user is typing into fields) or while they are
  // typing somewhere else (the composer, search).
  useEffect(() => {
    if (sent || busy || isForm) return undefined
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
  }, [rowsInfo.rows.length, customAllowed, sent, busy, isForm])

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
      id={cardId || undefined}
      className={`assistant-action-card ask-card${sent ? ' is-sent' : ''}`}
      data-kind="ask_user_question"
      aria-label={question}
      onKeyDown={onContainerKeyDown}
    >
      <div className="assistant-action-card-head">
        <div className="assistant-action-card-heading">
          <span className="assistant-action-card-icon" aria-hidden="true">
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none">
              <path
                d="M12 17v.5M9.1 9a3 3 0 1 1 5.82 1c-.5 1.7-2.42 2.1-2.92 3.5"
                stroke="currentColor"
                strokeWidth="1.8"
                strokeLinecap="round"
              />
              <circle cx="12" cy="12" r="9.2" stroke="currentColor" strokeWidth="1.6" />
            </svg>
          </span>
          <div className="assistant-action-card-heading-copy">
            <div className="assistant-action-card-kicker">Cosmic asks</div>
            <div className="assistant-action-card-title">{question}</div>
          </div>
        </div>
        <div className="assistant-action-card-head-actions">
          {counterLabel && <div className="assistant-action-card-status">{counterLabel}</div>}
          {sent ? (
            <div className="assistant-action-card-status is-sent">Answer sent</div>
          ) : onSkip ? (
            <button
              type="button"
              className="assistant-action-icon-button ask-card-dismiss"
              aria-label="Dismiss question"
              title="Skip this question"
              disabled={busy}
              onClick={() => onSkip()}
            >
              <svg width="11" height="11" viewBox="0 0 24 24" fill="none" aria-hidden="true">
                <path
                  d="M6 6l12 12M18 6L6 18"
                  stroke="currentColor"
                  strokeWidth="2.2"
                  strokeLinecap="round"
                />
              </svg>
            </button>
          ) : null}
        </div>
      </div>
      {context && <div className="ask-card-context">{context}</div>}
      {isForm ? (
        <div className="ask-card-rows">
          {formFields.map((field, index) => {
            const key = `${index}-${field.label}`
            if (field.kind === 'single' || field.kind === 'multi') {
              const picks = formPicks[index] || []
              const isSingle = field.kind === 'single'
              return (
                <div key={key} className="ask-card-row" role="group" aria-label={field.label}>
                  <span className="ask-card-row-label">{field.label}</span>
                  <div className={isSingle ? 'ask-card-row-choices' : 'ask-card-row-checks'}>
                    {field.options.map((option, optionIndex) => {
                      const active = picks.includes(optionIndex)
                      if (isSingle) {
                        return (
                          <button
                            key={option}
                            type="button"
                            className={`ask-card-chip${active ? ' is-selected' : ''}`}
                            aria-pressed={active}
                            disabled={busy || sent}
                            onClick={() => selectChoiceRow(index, optionIndex)}
                          >
                            {option}
                          </button>
                        )
                      }
                      return (
                        <button
                          key={option}
                          type="button"
                          className={`ask-card-check${active ? ' is-selected' : ''}`}
                          role="checkbox"
                          aria-checked={active}
                          disabled={busy || sent}
                          onClick={() => toggleMultiRow(index, optionIndex)}
                        >
                          <span className="ask-card-check-box" aria-hidden="true">
                            {active && (
                              <svg width="10" height="10" viewBox="0 0 24 24" fill="none">
                                <path
                                  d="M5 12.5l4.5 4.5L19 7.5"
                                  stroke="currentColor"
                                  strokeWidth="3"
                                  strokeLinecap="round"
                                  strokeLinejoin="round"
                                />
                              </svg>
                            )}
                          </span>
                          <span className="ask-card-check-label">{option}</span>
                        </button>
                      )
                    })}
                  </div>
                </div>
              )
            }
            return (
              <label key={key} className="ask-card-row">
                <span className="ask-card-row-label">{field.label}</span>
                <input
                  className="ask-card-row-input"
                  type="text"
                  value={formTexts[index] ?? ''}
                  autoComplete="off"
                  placeholder={field.placeholder || `Fill in ${field.label.toLowerCase()}…`}
                  disabled={busy || sent}
                  spellCheck={false}
                  maxLength={240}
                  aria-label={field.label}
                  onChange={(event) => setTextRow(index, event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === 'Enter' && !event.shiftKey) {
                      event.preventDefault()
                      event.stopPropagation()
                      handleContinue()
                    }
                  }}
                />
              </label>
            )
          })}
        </div>
      ) : (
        <>
          <div className="ask-card-options" role="listbox" aria-label="Answer choices">
            {rowsInfo.rows.map((option, index) => {
              const isSelected = selected === index
              const parts = splitOptionLabel(option)
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
                  <span className="ask-card-option-label">
                    <span className="ask-card-option-lead">{parts.lead}</span>
                    {parts.tail && <span className="ask-card-option-tail">{parts.tail}</span>}
                  </span>
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
                <span className="ask-card-option-label">Custom answer</span>
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
        </>
      )}
      {resolvedError && <div className="assistant-action-card-error ask-card-error" role="alert">{resolvedError}</div>}
      <div className="assistant-action-card-actions ask-card-footer">
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
            className="assistant-action-button is-primary ask-card-continue"
            disabled={busy}
            onClick={handleContinue}
          >
            {busy ? 'Sending…' : 'Done'}
          </button>
        )}
      </div>
    </section>
  )
}
