import { Check, Copy, ExternalLink } from 'lucide-react'
import { useState } from 'react'
import { contentCardKicker } from './contentCards'
import { BrandMark } from './registry'
import type { ContentCardBlock, ContentCardSection } from './types'

const copyText = async (text: string) => {
  await navigator.clipboard.writeText(text)
}

const openApprovedUrl = (url: string) => {
  window.open(url, '_blank', 'noopener,noreferrer')
}

function CardSections({ sections }: { sections: ContentCardSection[] }) {
  return (
    <>
      {sections.map((section, index) => {
        if (section.type === 'text') {
          return <div key={index} className="assistant-content-card-text">{section.text}</div>
        }
        if (section.type === 'quote') {
          return <div key={index} className="assistant-action-card-preview">{section.text}</div>
        }
        if (section.type === 'code') {
          return (
            <pre key={index} className="assistant-content-card-code">
              <code>{section.code}</code>
            </pre>
          )
        }
        if (section.type === 'chips') {
          return (
            <div key={index} className="assistant-content-card-chips">
              {section.items.map((item) => (
                <span key={item} className="assistant-content-card-chip">{item}</span>
              ))}
            </div>
          )
        }
        if (section.type === 'list') {
          return (
            <ul key={index} className={section.style === 'checklist' ? 'assistant-content-card-list is-check' : 'assistant-content-card-list'}>
              {section.items.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          )
        }
        return (
          <div key={index} className="assistant-action-card-details">
            {section.rows.map((row) => (
              <div key={row.label}><span>{row.label}</span>{row.value}</div>
            ))}
          </div>
        )
      })}
    </>
  )
}

export function ContentCard({ block }: { block: ContentCardBlock }) {
  const [copied, setCopied] = useState(false)
  const [error, setError] = useState('')
  const copyAction = block.actions.find((item) => item.type === 'copy')
  const openAction = block.actions.find((item) => item.type === 'open_url')
  const variant = block.group?.index && block.group?.total
    ? `${block.group.index} / ${block.group.total}`
    : null
  const countLabel = block.characterLimit
    ? `${block.characterCount ?? 0} / ${block.characterLimit}`
    : null
  const overLimit = Boolean(block.characterLimit && (block.characterCount ?? 0) > block.characterLimit)

  const onCopy = async () => {
    if (!copyAction) return
    setError('')
    try {
      await copyText(copyAction.text)
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1600)
    } catch {
      setError('Unable to copy.')
    }
  }

  return (
    <section className="assistant-action-card assistant-content-card" data-kind="content_card" data-preset={block.preset || 'generic'} data-brand={block.brand || 'generic'}>
      <div className="assistant-action-card-head">
        <div className="assistant-action-card-heading">
          <span className="assistant-action-card-icon" aria-hidden="true">
            <BrandMark brand={block.brand} size={15} />
          </span>
          <div className="assistant-action-card-heading-copy">
            <div className="assistant-action-card-kicker">{contentCardKicker(block)}</div>
            <div className="assistant-action-card-title">{block.title}</div>
          </div>
        </div>
        <div className="assistant-action-card-head-actions">
          {countLabel && (
            <div className={`assistant-action-card-status${overLimit ? ' is-over' : ''}`}>{countLabel}</div>
          )}
          {variant && !countLabel && (
            <div className="assistant-action-card-status">{variant}</div>
          )}
          {copyAction && (
            <button
              type="button"
              className="assistant-action-icon-button"
              onClick={() => void onCopy()}
              title={copyAction.label}
              aria-label={copyAction.label}
            >
              {copied ? <Check size={13} /> : <Copy size={13} />}
            </button>
          )}
        </div>
      </div>
      {block.subtitle && <div className="assistant-content-card-subtitle">{block.subtitle}</div>}
      {block.preset === 'social_post' && block.body ? (
        <div className="assistant-action-card-body">
          <div className="assistant-content-card-post">{block.body}</div>
          {(block.tags?.length || block.mentions?.length) ? (
            <div className="assistant-content-card-chips">
              {[...(block.mentions || []), ...(block.tags || [])].map((item) => (
                <span key={item} className="assistant-content-card-chip">{item}</span>
              ))}
            </div>
          ) : null}
        </div>
      ) : (
        <div className="assistant-content-card-sections">
          <CardSections sections={block.sections} />
        </div>
      )}
      {error && <div className="assistant-action-card-error">{error}</div>}
      {(copyAction || openAction) && (
        <div className="assistant-action-card-actions">
          {openAction && (
            <button
              type="button"
              className="assistant-action-button"
              onClick={() => openApprovedUrl(openAction.url)}
            >
              <ExternalLink size={13} /> {openAction.label}
            </button>
          )}
          {copyAction && (
            <button
              type="button"
              className="assistant-action-button is-primary"
              onClick={() => void onCopy()}
            >
              {copied ? <Check size={13} /> : <Copy size={13} />} {copied ? 'Copied' : copyAction.label}
            </button>
          )}
        </div>
      )}
    </section>
  )
}

export function ContentCardStack({ cards }: { cards: ContentCardBlock[] }) {
  if (cards.length <= 1) {
    return cards[0] ? <ContentCard block={cards[0]} /> : null
  }
  return (
    <div className="assistant-content-card-stack">
      {cards.map((card) => (
        <ContentCard key={card.id} block={card} />
      ))}
    </div>
  )
}
