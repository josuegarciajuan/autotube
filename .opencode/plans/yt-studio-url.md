# Plan: YouTube Studio URL per channel

## Summary
Add `yt_studio_url` field to each channel. Store in DB, expose via API, display a "Ver YT Studio" link in the channel detail page and channels list page that opens in a new tab.

## Steps

### 1. DB Migration — `database/db_extended.py`

**Add column migration** (after line 407, right after `ch_mon_columns` loop):

```python
    # Add yt_studio_url column to channels (idempotent)
    if "yt_studio_url" not in existing_ch:
        try:
            conn.execute("ALTER TABLE channels ADD COLUMN yt_studio_url TEXT")
            logger.info("Migration: added yt_studio_url column to channels")
        except sqlite3.OperationalError:
            pass
```

**Seed URLs for existing channels** (after line 382, after canal4 seed block):

```python
    # Seed yt_studio_url for existing channels (only if not yet set)
    studio_urls = {
        "canal2": "https://studio.youtube.com/channel/UC32VJJKqpbiEExfEHYGxdNw/editing/profile",
        "canal3": "https://studio.youtube.com/channel/UCejkjoNtUs99-LPBEYC7rPQ/editing/profile",
        "canal4": "https://studio.youtube.com/channel/UC9IOZKc0O4mBJ_Vb1x7czPg/editing/profile",
        "canal5": "https://studio.youtube.com/channel/UCDZi5NrlYnncYVlnZ0O7wKA/editing/profile",
    }
    for slug, url in studio_urls.items():
        conn.execute(
            "UPDATE channels SET yt_studio_url = ? WHERE slug = ? AND yt_studio_url IS NULL",
            (url, slug),
        )
    logger.info("Migration: seeded yt_studio_url for existing channels")
```

### 2. API Models — `api/schemas/models.py`

Add `yt_studio_url: Optional[str] = None` to three Pydantic models:

**ChannelUpdate** (after `google_account`, ~line 114):
```python
    yt_studio_url: Optional[str] = None
```

**ChannelResponse** (after `google_account`, ~line 128):
```python
    yt_studio_url: Optional[str] = None
```

**ChannelConfigUpdate** (after `google_account`, ~line 140):
```python
    yt_studio_url: Optional[str] = None
```

### 3. API Router — `api/routers/channels.py`

**update_channel()** — Add `yt_studio_url` to the profile fields tuple at line 104:

Change:
```python
for k in ("banner_url", "avatar_url", "description", "yt_channel_id", "yt_channel_url"):
```
To:
```python
for k in ("banner_url", "avatar_url", "description", "yt_channel_id", "yt_channel_url", "yt_studio_url"):
```

The `update_channel_profile()` endpoint uses `model_dump(exclude_none=True)` so it auto-handles new fields in `ChannelConfigUpdate` — no changes needed.

GET endpoints use `SELECT *` — no changes needed.

### 4. Frontend Types — `frontend/src/types/channel.ts`

Add to `Channel` interface (~line 87, after `google_account`):
```typescript
  yt_studio_url: string | null
```

### 5. Frontend ChannelDetail — `frontend/src/pages/ChannelDetail.tsx`

**A) Add to profileForm state** (line 209):
Add `yt_studio_url: ''` to the initial state object.

**B) Add to effect** (line 307):
Add `yt_studio_url: ch.yt_studio_url || ''` to the `setProfileForm` call.

**C) Add YT Studio button in channel header** (after line 877, after the YouTube button block):
```tsx
            {channel.yt_studio_url && (
              <a href={channel.yt_studio_url} target="_blank" rel="noopener noreferrer"
                className="flex items-center gap-1 px-2.5 sm:px-3 py-1.5 bg-neon-cyan/10 border border-neon-cyan/30 text-neon-cyan rounded-full text-xs font-medium hover:bg-neon-cyan/20 transition-colors">
                <ExternalLink size={14} /> <span className="hidden sm:inline">YT Studio</span>
              </a>
            )}
```

**D) Add input field in Edit Profile form** (after the google_account field, ~line 1659):
```tsx
              <div><label className="block text-xs text-gray-400 mb-1">URL de YouTube Studio</label>
                <input type="text" value={profileForm.yt_studio_url || ''} onChange={e => setProfileForm({ ...profileForm, yt_studio_url: e.target.value })}
                  className="w-full px-3 py-2 bg-dark-700 border border-surface-border rounded-lg text-white text-sm focus:outline-none focus:border-neon-red" /></div>
```

### 6. Frontend Channels list — `frontend/src/pages/Channels.tsx`

Add a "YT Studio" link button in channel cards (around line 285-294, next to the existing "YT" / "—" buttons). Change the 2-column grid to accommodate an extra button, or add it in the same row.

Option: Change the grid-cols-2 to grid-cols-3 and add:

```tsx
                {ch.yt_studio_url ? (
                  <a href={ch.yt_studio_url} target="_blank" rel="noopener noreferrer"
                    className="text-center px-3 py-1.5 bg-neon-cyan/10 border border-neon-cyan/30 text-neon-cyan rounded-lg text-xs font-medium hover:bg-neon-cyan/20 transition-colors flex items-center justify-center gap-1">
                    <ExternalLink size={12} /> Studio
                  </a>
                ) : (
                  <span className="text-center px-3 py-1.5 text-gray-600 text-xs flex items-center justify-center gap-1 cursor-not-allowed">
                    <ExternalLink size={12} /> —
                  </span>
                )}
```

### 7. Commit

Format: `feat: add yt_studio_url field per channel with UI link`

## Verification

After implementation, the DB migration adds the column on next API start. The URLs are seeded for existing channels. The frontend shows:
- ChannelDetail header: "YT Studio" button (opens in new tab) next to the "YouTube" button
- Channels list: "Studio" link in each channel card
- Edit Profile form: input field for YT Studio URL
