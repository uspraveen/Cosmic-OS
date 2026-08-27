// Shared glass surface tones.
//
// SPACES_BLACK_GLASS_* is derived from spaces-control.css's
// --spaces-aurora-panel (black glass + blue aurora + faint sheen), with one
// settings-specific adjustment: the aurora uses a fixed 190px height instead
// of a percentage ray, because the settings panel is much smaller than the
// Spaces shell and the original ray stretched the glow too far down.

export const SPACES_BLACK_GLASS_BACKGROUND = [
  'radial-gradient(ellipse 130% 190px at 50% 0%, rgba(121, 201, 255, 0.14), transparent 72%)',
  'linear-gradient(180deg, rgba(255, 255, 255, 0.028), rgba(255, 255, 255, 0.008))',
  'rgba(0, 0, 0, 0.62)',
].join(', ')

export const SPACES_BLACK_GLASS_BACKDROP = 'blur(24px) saturate(140%)'

