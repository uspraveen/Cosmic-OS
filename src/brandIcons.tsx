// Official brand marks for the Alpha provider surfaces.
// Sources: OpenAI mark = official OpenAI logo geometry; Cursor = "General
// Logos/Cube/SVG/CUBE_25D.svg" from the official cursor-brand-assets pack;
// OpenCode = "Logo/opencode-logo-{light,dark}.svg" from the official
// opencode-brand-assets pack. Fill-based so tiles recolor OpenAI via
// currentColor; Cursor/OpenCode use their official colors.

export function OpenAIMark({ size = 20 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
      <path d="M22.2819 9.8211a5.9847 5.9847 0 0 0-.5157-4.9108 6.0462 6.0462 0 0 0-6.5098-2.9A6.0651 6.0651 0 0 0 4.9807 4.1818a5.9847 5.9847 0 0 0-3.9977 2.9 6.0462 6.0462 0 0 0 .7427 7.0966 5.98 5.98 0 0 0 .511 4.9107 6.051 6.051 0 0 0 6.5146 2.9001A5.9847 5.9847 0 0 0 13.2599 24a6.0557 6.0557 0 0 0 5.7718-4.2058 5.9894 5.9894 0 0 0 3.9977-2.9001 6.0557 6.0557 0 0 0-.7475-7.0729zm-9.022 12.6081a4.4755 4.4755 0 0 1-2.8764-1.0408l.1419-.0804 4.7783-2.7582a.7948.7948 0 0 0 .3927-.6813v-6.7369l2.02 1.1686a.071.071 0 0 1 .038.052v5.5826a4.504 4.504 0 0 1-4.4945 4.4944zm-9.6607-4.1254a4.4708 4.4708 0 0 1-.5346-3.0137l.142.0852 4.783 2.7582a.7712.7712 0 0 0 .7806 0l5.8428-3.3685v2.3324a.0804.0804 0 0 1-.0332.0615L9.74 19.9502a4.4992 4.4992 0 0 1-6.1408-1.6464zM2.3408 7.8956a4.485 4.485 0 0 1 2.3655-1.9728V11.6a.7664.7664 0 0 0 .3879.6765l5.8144 3.3543-2.0201 1.1685a.0757.0757 0 0 1-.071 0l-4.8303-2.7865A4.504 4.504 0 0 1 2.3408 7.872zm16.5963 3.8558L13.1038 8.364 15.1192 7.2a.0757.0757 0 0 1 .071 0l4.8303 2.7913a4.4944 4.4944 0 0 1-.6765 8.1042v-5.6772a.79.79 0 0 0-.407-.667zm2.0107-3.0231l-.142-.0852-4.7735-2.7818a.7759.7759 0 0 0-.7854 0L9.409 9.2297V6.8974a.0662.0662 0 0 1 .0284-.0615l4.8303-2.7866a4.4992 4.4992 0 0 1 6.6802 4.66zM8.3065 12.863l-2.02-1.1638a.0804.0804 0 0 1-.038-.0567V6.0742a4.4992 4.4992 0 0 1 7.3757-3.4537l-.142.0805L8.704 5.459a.7948.7948 0 0 0-.3927.6813zm1.0976-2.3654l2.602-1.4998 2.6069 1.4998v2.9994l-2.5974 1.4997-2.6067-1.4997Z" />
    </svg>
  )
}

export function CursorMark({ size = 20, mono = false }: { size?: number; mono?: boolean }) {
  if (mono) {
    // Official single-path 2D cube (CUBE_2D_LIGHT.svg) recolored via
    // currentColor — the colored cube's white top face would vanish on the
    // toggle's white active pill.
    return (
      <svg width={size} height={size} viewBox="0 0 466.73 532.09" fill="currentColor" aria-hidden="true">
        <path d="M457.43,125.94L244.42,2.96c-6.84-3.95-15.28-3.95-22.12,0L9.3,125.94c-5.75,3.32-9.3,9.46-9.3,16.11v247.99c0,6.65,3.55,12.79,9.3,16.11l213.01,122.98c6.84,3.95,15.28,3.95,22.12,0l213.01-122.98c5.75-3.32,9.3-9.46,9.3-16.11v-247.99c0-6.65-3.55-12.79-9.3-16.11h-.01ZM444.05,151.99l-205.63,356.16c-1.39,2.4-5.06,1.42-5.06-1.36v-233.21c0-4.66-2.49-8.97-6.53-11.31L24.87,145.67c-2.4-1.39-1.42-5.06,1.36-5.06h411.26c5.84,0,9.49,6.33,6.57,11.39h-.01Z" />
      </svg>
    )
  }
  return (
    <svg width={size} height={size} viewBox="0 0 466.73 533.32" fill="none" aria-hidden="true">
      <path fill="#72716d" d="M233.37 266.66 464.53 400.12c-1.42 2.46-3.48 4.56-6.03 6.03L242.44 530.89c-5.61 3.24-12.53 3.24-18.14 0L8.24 406.15c-2.55-1.47-4.61-3.57-6.03-6.03L233.37 266.66Z" />
      <path fill="#55544f" d="M233.37 0v266.66L2.21 400.12c-1.42-2.46-2.21-5.3-2.21-8.24V141.44c0-5.89 3.14-11.32 8.24-14.27L224.29 2.43c2.81-1.62 5.94-2.43 9.07-2.43h.01Z" />
      <path fill="#43413c" d="M464.52 133.2c-1.42-2.46-3.48-4.56-6.03-6.03L242.43 2.43c-2.8-1.62-5.93-2.43-9.06-2.43v266.66l231.16 133.46c1.42-2.46 2.21-5.3 2.21-8.24V141.44c0-2.95-.78-5.77-2.21-8.24h-.01Z" />
      <path fill="#d6d5d2" d="M448.35 142.54c1.31 2.26 1.49 5.16 0 7.74L238.52 513.7c-1.41 2.46-5.16 1.45-5.16-1.38V272.84c0-1.91-.51-3.75-1.44-5.36l216.42-124.95h.01Z" />
      <path fill="#ffffff" d="M448.35 142.54 231.93 267.49c-.92-1.6-2.26-2.96-3.92-3.92L20.62 143.83c-2.46-1.41-1.45-5.16 1.38-5.16h419.65c2.98 0 5.4 1.61 6.7 3.87Z" />
    </svg>
  )
}

export function OpenCodeMark({
  size = 20,
  tone = 'dark',
}: {
  size?: number
  tone?: 'dark' | 'light'
}) {
  // 'dark' = for light backgrounds (official light logo); 'light' = for dark
  // backgrounds (official dark logo).
  const outer = tone === 'dark' ? '#211E1E' : '#F1ECEC'
  const inner = tone === 'dark' ? '#CFCECD' : '#4B4646'
  return (
    <svg width={size * 0.8} height={size} viewBox="0 0 240 300" fill="none" aria-hidden="true">
      <path d={`M180 240H60V120H180V240Z`} fill={inner} />
      <path d="M180 60H60V240H180V60ZM240 300H0V0H240V300Z" fill={outer} />
    </svg>
  )
}
