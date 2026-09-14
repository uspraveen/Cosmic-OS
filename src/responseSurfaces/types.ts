export type ContentCardPreset = 'social_post' | 'copy_payload' | 'option_set' | 'checklist'

export type ContentCardBrand = 'x' | 'gmail' | 'github' | 'generic'

export type ContentCardSection =
  | { type: 'text'; text: string }
  | { type: 'quote'; text: string }
  | { type: 'code'; code: string; language?: string | null }
  | { type: 'chips'; items: string[] }
  | { type: 'list'; items: string[]; style?: string | null }
  | { type: 'key_value'; rows: Array<{ label: string; value: string }> }

export type ContentCardAction =
  | { type: 'copy'; label: string; text: string }
  | { type: 'open_url'; label: string; url: string }

export interface ContentCardGroup {
  id: string
  index?: number | null
  total?: number | null
}

export interface ContentCardBlock {
  id: string
  type: 'content_card'
  schemaVersion: number
  preset?: ContentCardPreset | null
  brand?: ContentCardBrand | null
  title: string
  subtitle?: string | null
  body?: string | null
  sections: ContentCardSection[]
  actions: ContentCardAction[]
  tags?: string[]
  mentions?: string[]
  characterCount?: number | null
  characterLimit?: number | null
  group?: ContentCardGroup | null
  fallbackText?: string | null
}
