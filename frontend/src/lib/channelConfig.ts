/** Channel display configuration — single source of truth for all UI components.
 *
 *  When a new channel is added to the database, add its entry here to ensure
 *  it renders with the correct abbreviation, dot color, and style across all
 *  scheduling / pipeline / planning views.
 */

// ── 3-letter abbreviation (used in all pipeline & schedule components) ──
export const CHANNEL_SHORT: Record<string, string> = {
  canal2: 'SIN',
  canal3: 'CIV',
  canal4: 'EXP',
  canal5: 'ANM',
  test: 'TST',
};

// ── Dot (status indicator) color — shared across views ────────────────
export const CHANNEL_DOT: Record<string, string> = {
  canal2: 'bg-neon-cyan',
  canal3: 'bg-amber-400',
  canal4: 'bg-purple-400',
  canal5: 'bg-neon-red',
  test: 'bg-gray-400',
};

// ── Full channel style object — used by PipelineView & Scheduling ─────
export interface ChannelStyle {
  dot: string;
  text: string;
  bg: string;
  border: string;
  accent?: string;
}

export const CHANNEL_STYLES: Record<string, ChannelStyle> = {
  canal2: {
    dot: 'bg-neon-cyan',
    text: 'text-neon-cyan',
    bg: 'bg-neon-cyan/10',
    border: 'border-neon-cyan/30',
    accent: 'neon-cyan',
  },
  canal3: {
    dot: 'bg-amber-400',
    text: 'text-amber-400',
    bg: 'bg-amber-400/10',
    border: 'border-amber-400/30',
    accent: 'amber-400',
  },
  canal4: {
    dot: 'bg-purple-400',
    text: 'text-purple-400',
    bg: 'bg-purple-400/10',
    border: 'border-purple-400/30',
    accent: 'purple-400',
  },
  canal5: {
    dot: 'bg-neon-red',
    text: 'text-neon-red',
    bg: 'bg-neon-red/10',
    border: 'border-neon-red/30',
    accent: 'neon-red',
  },
  test: {
    dot: 'bg-gray-400',
    text: 'text-gray-400',
    bg: 'bg-gray-400/10',
    border: 'border-gray-400/30',
  },
};

// ── Pill-style (string) class — used by DailySchedule & UpcomingExecutions ──
export const CHANNEL_PILL: Record<string, string> = {
  canal2: 'bg-neon-cyan/20 text-neon-cyan border-neon-cyan/30',
  canal3: 'bg-amber-400/20 text-amber-400 border-amber-400/30',
  canal4: 'bg-purple-400/20 text-purple-400 border-purple-400/30',
  canal5: 'bg-neon-red/20 text-neon-red border-neon-red/30',
  test: 'bg-gray-400/20 text-gray-400 border-gray-400/30',
};

// ── Table-row style — used by ChannelScheduleTable ────────────────────
export const CHANNEL_TABLE_ROW: Record<string, { bg: string; text: string; border: string; dot: string }> = {
  canal2: { bg: 'bg-neon-cyan/15', text: 'text-neon-cyan', border: 'border-neon-cyan/30', dot: 'bg-neon-cyan' },
  canal3: { bg: 'bg-amber-400/15', text: 'text-amber-400', border: 'border-amber-400/30', dot: 'bg-amber-400' },
  canal4: { bg: 'bg-purple-400/15', text: 'text-purple-400', border: 'border-purple-400/30', dot: 'bg-purple-400' },
  canal5: { bg: 'bg-neon-red/15', text: 'text-neon-red', border: 'border-neon-red/30', dot: 'bg-neon-red' },
  test: { bg: 'bg-gray-400/15', text: 'text-gray-400', border: 'border-gray-400/30', dot: 'bg-gray-400' },
};

// ── Default / fallback for unknown channels ───────────────────────────
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
