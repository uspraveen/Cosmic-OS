// Shared glass surface tones.
//
// SPACES_BLACK_GLASS_* mirrors spaces-control.css's --spaces-aurora-panel:
// the black glassy tint of the Spaces control center — pure black glass with
// a soft blue aurora rising from the top edge and a faint white sheen. Keep
// the two definitions in sync if either changes.

export const SPACES_BLACK_GLASS_BACKGROUND = [
  'radial-gradient(circle at 50% 0%, rgba(121, 201, 255, 0.14), transparent 58%)',
  'linear-gradient(180deg, rgba(255, 255, 255, 0.028), rgba(255, 255, 255, 0.008))',
  'rgba(0, 0, 0, 0.62)',
].join(', ')

export const SPACES_BLACK_GLASS_BACKDROP = 'blur(24px) saturate(140%)'
