import type { ReactNode } from 'react'
import { Github, Layers, Mail } from 'lucide-react'
import { XMark } from '../brandIcons'
import type { ContentCardBrand } from './types'

export function BrandMark({ brand, size = 15 }: { brand?: ContentCardBrand | null; size?: number }): ReactNode {
  if (brand === 'x') return <XMark size={size} />
  if (brand === 'gmail') return <Mail size={size} />
  if (brand === 'github') return <Github size={size} />
  return <Layers size={size} />
}
