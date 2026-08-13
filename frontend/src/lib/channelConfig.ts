/** Channel display configuration — derived dynamically, no hardcoded slugs.
 *
 *  Colors are assigned from a rotating palette based on the channel id.
 *  Abbreviations come from CANAL_INITIALS in the channel config (or auto-derived).
 *  All functions accept a channel object with at minimum { slug, id }.
 */

// ── Color palette (8 colors, rotated by channel.id) ──────────────────
const PALETTE = [
  { name: 'cyan',     dot: 'bg-neon-cyan',    text: 'text-neon-cyan',    bg: 'bg-neon-cyan/10',    border: 'border-neon-cyan/30',    pill_bg: 'bg-neon-cyan/20',    table_bg: 'bg-neon-cyan/15' },
  { name: 'amber',    dot: 'bg-amber-400',     text: 'text-amber-400',    bg: 'bg-amber-400/10',    border: 'border-amber-400/30',    pill_bg: 'bg-amber-400/20',    table_bg: 'bg-amber-400/15' },
  { name: 'purple',   dot: 'bg-purple-400',    text: 'text-purple-400',   bg: 'bg-purple-400/10',   border: 'border-purple-400/30',   pill_bg: 'bg-purple-400/20',   table_bg: 'bg-purple-400/15' },
  { name: 'red',      dot: 'bg-neon-red',      text: 'text-neon-red',     bg: 'bg-neon-red/10',     border: 'border-neon-red/30',     pill_bg: 'bg-neon-red/20',     table_bg: 'bg-neon-red/15' },
  { name: 'emerald',  dot: 'bg-emerald-400',   text: 'text-emerald-400',  bg: 'bg-emerald-400/10',  border: 'border-emerald-400/30',  pill_bg: 'bg-emerald-400/20',  table_bg: 'bg-emerald-400/15' },
  { name: 'sky',      dot: 'bg-sky-400',       text: 'text-sky-400',      bg: 'bg-sky-400/10',      border: 'border-sky-400/30',      pill_bg: 'bg-sky-400/20',      table_bg: 'bg-sky-400/15' },
  { name: 'pink',     dot: 'bg-pink-400',      text: 'text-pink-400',     bg: 'bg-pink-400/10',     border: 'border-pink-400/30',     pill_bg: 'bg-pink-400/20',     table_bg: 'bg-pink-400/15' },
  { name: 'orange',   dot: 'bg-orange-400',    text: 'text-orange-400',   bg: 'bg-orange-400/10',   border: 'border-orange-400/30',   pill_bg: 'bg-orange-400/20',   table_bg: 'bg-orange-400/15' },
];

interface ChannelLike {
  id?: number;
  slug?: string;
  channel_id?: number;
  channel_slug?: string;
  canal_initials?: string;
  channel_name?: string;
  name?: string;
}

function _slug(ch: ChannelLike): string {
  return (ch as any).slug || (ch as any).channel_slug || '';
}

function _id(ch: ChannelLike): number {
  return (ch as any).id || (ch as any).channel_id || 0;
}

function _color(ch: ChannelLike) {
  // Deterministic: use channel id to pick from palette, fallback to hash of slug
  const id = _id(ch);
  const index = id > 0 ? (id - 1) % PALETTE.length : _hashSlug(_slug(ch)) % PALETTE.length;
  return PALETTE[index];
}

function _hashSlug(slug: string): number {
  let hash = 0;
  for (let i = 0; i < slug.length; i++) {
    hash = ((hash << 5) - hash) + slug.charCodeAt(i);
    hash |= 0;
  }
  return Math.abs(hash);
}

// ── 3-letter abbreviation ────────────────────────────────────────────
function _initialsFromName(name: unknown): string | null {
  if (!name || typeof name !== 'string') return null;
  const words = name.trim().split(/\s+/).filter(Boolean);
  if (words.length === 0) return null;
  if (words.length === 1) {
    const w = words[0].replace(/[^a-zA-Z0-9]/g, '');
    return w.slice(0, 3).toUpperCase() || null;
  }
  const letters = words.slice(0, 3).map(w => (w[0] || '').toUpperCase()).join('');
  return letters || null;
}

export function getChannelShort(ch: ChannelLike): string {
  const initials = (ch as any).canal_initials;
  if (initials && typeof initials === 'string' && initials.length >= 2) return initials.substring(0, 3).toUpperCase();
  const slug = _slug(ch);
  // Check the pre-populated map (set by GenerationContext.initAllChannelMaps with canal_initials from API)
  if (slug && CHANNEL_SHORT[slug]) return CHANNEL_SHORT[slug];
  // Derive from the channel name (never the slug, which yields 'CAN' for every canalX)
  const fromName = _initialsFromName((ch as any).channel_name || (ch as any).name);
  if (fromName) return fromName;
  if (!slug) return '???';
  return slug.toUpperCase() || 'CHN';
}

// Backward-compat map (derived from channel list at runtime)
export const CHANNEL_SHORT: Record<string, string> = {};

export function initChannelShort(channels: ChannelLike[]) {
  for (const ch of channels) {
    const slug = _slug(ch);
    const initials = (ch as any).canal_initials;
    if (initials && typeof initials === 'string' && initials.length >= 2) {
      CHANNEL_SHORT[slug] = initials.substring(0, 3).toUpperCase();
    } else {
      // Fallback: derive from the display name (e.g. 'Sincronías' → 'SIN'),
      // never from the slug (which collides as 'CAN' for every canalX channel).
      CHANNEL_SHORT[slug] =
        _initialsFromName((ch as any).channel_name || (ch as any).name) ||
        slug.toUpperCase() ||
        slug;
    }
  }
}

// ── Dot color ────────────────────────────────────────────────────────
export function getChannelDot(ch: ChannelLike): string {
  return _color(ch).dot;
}

export const CHANNEL_DOT: Record<string, string> = {};

export function initChannelDot(channels: ChannelLike[]) {
  for (const ch of channels) {
    CHANNEL_DOT[_slug(ch)] = getChannelDot(ch);
  }
}

// ── Full channel style ────────────────────────────────────────────────
export interface ChannelStyle {
  dot: string;
  text: string;
  bg: string;
  border: string;
  accent?: string;
}

export function getChannelStyles(ch: ChannelLike): ChannelStyle {
  const c = _color(ch);
  return { dot: c.dot, text: c.text, bg: c.bg, border: c.border, accent: c.name };
}

export const CHANNEL_STYLES: Record<string, ChannelStyle> = {};

export function initChannelStyles(channels: ChannelLike[]) {
  for (const ch of channels) {
    CHANNEL_STYLES[_slug(ch)] = getChannelStyles(ch);
  }
}

// ── Pill style ────────────────────────────────────────────────────────
export function getChannelPill(ch: ChannelLike): string {
  const c = _color(ch);
  return `${c.pill_bg} ${c.text} ${c.border}`;
}

export const CHANNEL_PILL: Record<string, string> = {};

export function initChannelPill(channels: ChannelLike[]) {
  for (const ch of channels) {
    CHANNEL_PILL[_slug(ch)] = getChannelPill(ch);
  }
}

// ── Table row style ───────────────────────────────────────────────────
export function getChannelTableRow(ch: ChannelLike): { bg: string; text: string; border: string; dot: string } {
  const c = _color(ch);
  return { bg: c.table_bg, text: c.text, border: c.border, dot: c.dot };
}

export const CHANNEL_TABLE_ROW: Record<string, { bg: string; text: string; border: string; dot: string }> = {};

export function initChannelTableRow(channels: ChannelLike[]) {
  for (const ch of channels) {
    CHANNEL_TABLE_ROW[_slug(ch)] = getChannelTableRow(ch);
  }
}

// ── Initialize all maps at once (call once after fetching channels) ──
export function initAllChannelMaps(channels: ChannelLike[]) {
  initChannelShort(channels);
  initChannelDot(channels);
  initChannelStyles(channels);
  initChannelPill(channels);
  initChannelTableRow(channels);
}

// ── Default / fallback styles (unchanged) ────────────────────────────
export const DEFAULT_STYLE: ChannelStyle = {
  dot: 'bg-gray-400',
  text: 'text-gray-400',
  bg: 'bg-gray-500/20',
  border: 'border-gray-500/30',
};

export const DEFAULT_PILL = 'bg-gray-500/20 text-gray-400 border-gray-500/30';

export const DEFAULT_TABLE_ROW = {
  bg: 'bg-gray-500/15',
  text: 'text-gray-400',
  border: 'border-gray-500/30',
  dot: 'bg-gray-400',
};
