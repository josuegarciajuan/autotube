# Plan: Granular Progress Bars for Video Generation

## Summary

Add intermediate progress callbacks at strategic choke points during video/short
generation to make the progress bar move more smoothly instead of getting "stuck"
at certain percentages (currently 7%, 30%, 45%, 60%).

## Files to modify (8)

| File | Changes |
|------|---------|
| `orchestrator.py` | 4 phases: scrape stagger, TTS per-block, media per-scene, video concat |
| `pipeline/tts_engine.py` | Already has `progress_cb` — just verify wiring |
| `pipeline/media_fetcher.py` | Add `progress_cb` param to `fetch_for_script()` |
| `pipeline/video_editor.py` | Route segment-progress via callback + add concat progress |
| `pipeline/shorts_media.py` | Add `progress_cb` to `render_short_hybrid()` + `fetch_short_assets_exhaustive()` |
| `api/services/shorts_scheduler.py` | Pass progress_cb to TTS, media, render functions |
| `api/services/full_pipeline_worker.py` | Minor: wire video_id to build_video for progress + sync milestones |
| `api/services/generation_service.py` | Minor: sync milestone broadcasts |

---

## Change 1: `orchestrator.py` — Stagger scrape progress

**Location:** `phase_scrape()` at line 323

**Current:** All scrapers emit `progress=7`.

**Change:** Calculate active scraper count, stagger from 6 to 9.

```python
# REPLACE lines 321-345 in phase_scrape():
# OLD:
        # Reddit scraping with per-source timeout (5 min per scraper)
        # Viral scraper: skip in non-viral mode (runs on-demand in _phase_generate_script_viral)
        for scraper_name, s in self.scraper.items():
            if scraper_name == "youtube_viral" and self.source_mode != "viral":
                logger.debug("[%s] Skipping viral scraper in %s mode", self.canal, self.source_mode)
                continue
            try:
                timeout = 600 if scraper_name == "youtube_viral" else 300
                progress_msg = (
                    f"Buscando videos virales en YouTube ({scraper_name})..."
                    if scraper_name == "youtube_viral"
                    else f"Scraping {scraper_name}..."
                )
                self._emit_progress(7, "scrape", progress_msg)
                ...
# NEW:
        # Reddit scraping with per-source timeout (5 min per scraper)
        # Viral scraper: skip in non-viral mode (runs on-demand in _phase_generate_script_viral)
        scraper_names = list(self.scraper.keys())
        active_scrapers = [n for n in scraper_names
                          if not (n == "youtube_viral" and self.source_mode != "viral")]
        n_active = len(active_scrapers) if active_scrapers else 1
        scraper_idx = 0
        for scraper_name, s in self.scraper.items():
            if scraper_name == "youtube_viral" and self.source_mode != "viral":
                logger.debug("[%s] Skipping viral scraper in %s mode", self.canal, self.source_mode)
                continue
            # Stagger progress per scraper: 6→9% range
            scraper_pct = 6 + int((scraper_idx / n_active) * 3)
            scraper_idx += 1
            try:
                timeout = 600 if scraper_name == "youtube_viral" else 300
                progress_msg = (
                    f"Buscando videos virales en YouTube ({scraper_name})..."
                    if scraper_name == "youtube_viral"
                    else f"Scraping {scraper_name}..."
                )
                self._emit_progress(scraper_pct, "scrape", progress_msg)
                ...
```

---

## Change 2: `orchestrator.py` — Per-block TTS progress

**Location:** `phase_tts()` at line 856

**Current:** `self.tts.generate_segmented(bloques)` called with no progress_cb.

**Change:** Pass a lambda that maps block_i/total to 31-37%.

```python
# REPLACE line 856:
# OLD:
                    audio_path, timestamps = self.tts.generate_segmented(bloques)
# NEW:
                    tts_total = len(bloques)
                    def _tts_progress(i_block: int, total: int):
                        if total > 0:
                            pct = 31 + int((i_block / total) * 6)
                            self._emit_progress(pct, "tts",
                                f"Generando voz: bloque {i_block}/{total}...")
                    audio_path, timestamps = self.tts.generate_segmented(
                        bloques, progress_cb=_tts_progress,
                    )
```

**Also verify** `pipeline/tts_engine.py:generate_segmented()` already calls `progress_cb(i+1, len(bloques))` at line 380-384 — it does. No changes needed in tts_engine.py.

---

## Change 3: `pipeline/media_fetcher.py` — Per-scene media progress

**Location:** `fetch_for_script()` — add `progress_cb` param and call it in the fetch loop.

**Current:** No progress callback. Fetches 50-200 scenes silently.

**Change A — `media_fetcher.py`:** Add `progress_cb` parameter to `fetch_for_script()`

```python
# At line 384, modify signature:
    def fetch_for_script(
        self,
        bloques: list[dict] = None,
        theme_context=None,
        scene_ranges: list[dict] | None = None,
        progress_cb: callable = None,          # <-- NEW
    ) -> list[dict]:

# In the fetch loop, after line 672 (results[i] = asset):
# Add progress call every ~10% of scenes:
            results[i] = asset
            
            # Progress callback (every 10% of scenes)
            if progress_cb is not None and (i == 0 or i == n_scenes - 1
                    or (i + 1) % max(1, n_scenes // 10) == 0):
                try:
                    progress_cb(i + 1, n_scenes)
                except Exception:
                    pass
```

**Change B — `orchestrator.py:phase_media()`:** Pass progress_cb to media_fetcher

```python
# At line 987, REPLACE:
            media_assets = self.media_fetcher.fetch_for_script(
                bloques=bloques,
                scene_ranges=scene_ranges,
            )
# WITH:
            n_total = len(scene_ranges) if scene_ranges else len(bloques)
            def _media_progress(i_scene: int, total: int):
                if total > 0:
                    pct = 46 + int((i_scene / total) * 6)
                    self._emit_progress(pct, "images",
                        f"Descargando media: {i_scene}/{total}...")
            media_assets = self.media_fetcher.fetch_for_script(
                bloques=bloques,
                scene_ranges=scene_ranges,
                progress_cb=_media_progress,
            )
```

---

## Change 4: `pipeline/video_editor.py` — Concat progress + callback routing

**Current:** Segment rendering writes DB progress directly (only when video_id set).
The ffmpeg xfade concat (Step 3/6) and final assembly (Steps 4-6/6) have NO progress.

**Change A — `video_editor.py:build_video()`:** Add `progress_cb` param

```python
# At line 278, modify signature:
    def build_video(
        self,
        bloques: list[dict] = None,
        media_assets: list[dict] = None,
        ...
        video_id: int = None,
        progress_cb: callable = None,           # <-- NEW
    ) -> Path:
```

**Change B — Route segment-rendering progress through callback:**

In the segment rendering loop (around line 566-577), after the existing DB write, also call progress_cb:

```python
# After line 577 (or after the DB update block), ADD:
            if progress_cb is not None:
                try:
                    progress_cb(_pct, "video", 
                        f"Renderizando escenas: {_total_done}/{_total}...")
                except Exception:
                    pass
```

**Change C — Add progress before/after ffmpeg concat (Steps 3-6/6):**

```python
# BEFORE Step 3 (line 711), ADD:
        if progress_cb is not None:
            progress_cb(69, "video", "Concatenando segmentos con transiciones...")
        
        # (existing code: Step 3/6 concat)
        self.logger.info("Step 3/6: Concatenating %d segments with xfade…", len(segment_paths))
        body_path = seg_dir / "body.mp4"
        body_segment_path = self._concat_body_with_crossfades(...)

# AFTER Step 3, BEFORE Step 4, ADD:
        if progress_cb is not None:
            progress_cb(71, "video", "Video base ensamblado, añadiendo CTA...")

# BEFORE Step 5 (line 815), ADD:
        if progress_cb is not None:
            progress_cb(73, "video", "Renderizando intro/outro + montaje final...")

# BEFORE Step 6 audio post-process (line 1112 area), ADD:
        if progress_cb is not None:
            progress_cb(74, "video", "Aplicando audio final...")
```

**Change D — `orchestrator.py:phase_video()`:** Pass progress_cb to build_video

```python
# At line 1136, ADD progress_cb parameter:
                video_path = self.video_editor.build_video(
                    bloques=bloques,
                    media_assets=media_assets,
                    audio_path=audio_data["audio_path"],
                    timestamps=audio_data["timestamps"],
                    scene_ranges=getattr(self, "_last_scene_ranges", None),
                    job_id=job_id,
                    cta_audio_path=audio_data.get("cta_audio_path"),
                    video_id=self.db_video_id,          # <-- NEW (already needed for segment progress)
                    progress_cb=self._emit_progress,    # <-- NEW
                )
```

---

## Change 5: `pipeline/shorts_media.py` — Progress for shorts render

**Current:** `render_short_hybrid()` has no progress_cb.

**Change A — `shorts_media.py:render_short_hybrid()`:** Add progress_cb param

```python
# At line 1071, modify signature:
def render_short_hybrid(
    asset_items: list[dict[str, Any] | None],
    audio_path: Path,
    output_path: Path,
    ...
    scene_ranges: list[dict] | None = None,
    progress_cb: callable = None,              # <-- NEW
) -> Path:

# At strategic points during the function:
# - After probing audio (around line 1135): progress_cb(55, "render", "Analizando audio...")
# - After building ffmpeg filter (before subprocess.run): progress_cb(65, "render", "Renderizando short...")
# - Before subtitle burn (if srt_path): progress_cb(70, "render", "Añadiendo subtítulos...")
```

**Change B — `shorts_media.py:fetch_short_assets_exhaustive()`:** Add progress_cb

```python
# At line 901, modify signature:
def fetch_short_assets_exhaustive(
    blocks: list[dict[str, Any]],
    ch_config: Any,
    ...
    channel_slug: str = "",
    progress_cb: callable = None,              # <-- NEW
) -> list[dict[str, Any]]:

# In the fetch loop (line 1009), after each block is fetched:
    for i, block in enumerate(blocks):
        ...existing code...
        assets[i] = asset
        
        # Progress callback
        if progress_cb is not None and (i == 0 or i == n_blocks - 1
                or (i + 1) % max(1, n_blocks // 3) == 0):
            try:
                progress_cb(i + 1, n_blocks)
            except Exception:
                pass
```

---

## Change 6: `api/services/shorts_scheduler.py` — Wire shorts progress

**Change A — Native short (`_dispatch_native_short`):**

Add TTS per-block progress and media/render progress callbacks.

```python
# After line 1919 (synthesize_shorts_blocks call), add progress wrapper:
# Note: synthesize_shorts_blocks uses generate_segmented which already
# accepts progress_cb. If shorts_tts doesn't pass it through, add it.

# Current (line 1921-1926):
        tts_result = synthesize_shorts_blocks(
            bloques=bloques,
            ch_config=ch_config,
            output_audio_path=audio_path,
            output_srt_path=srt_path,
        )

# → Add progress_cb support to synthesize_shorts_blocks or wrap it.
# For now, add intermediate emits between major steps:
# After TTS (line 1932 area): keep _update_short_job_progress(job_id, 25, "tts")
# But add one midway through TTS if possible.

# For media fetch (line 1974 area):
        asset_items = fetch_short_assets_exhaustive(
            fetch_list, ch_config, theme_kw,
            theme_ctx=theme_context,
            channel_id=channel_id, channel_slug=channel_slug,
            progress_cb=lambda i, t: _update_short_job_progress(
                job_id, 28 + int((i/t)*20), "media"
            ) if t > 0 else None,
        )

# For render (line 2014 area):
        render_short_hybrid(
            asset_items=render_assets,
            audio_path=audio_path,
            output_path=video_path,
            audio_duration=audio_duration,
            bg_color_hex=bg_color,
            srt_path=srt_path if srt_path.exists() else None,
            scene_ranges=render_ranges,
            progress_cb=lambda pct, phase, msg: _update_short_job_progress(
                job_id, pct, phase
            ),
        )
```

**Change B — Clip short (`_dispatch_clip_short`):**

```python
# After line 2511 (render call), ADD progress before render starts:
        _update_short_job_progress(job_id, 45, "render")
        
        output_path = renderer.render(
            source_path, best_clip, word_timestamps=render_word_ts,
        )
```

The clip short render is a single ffmpeg command, so it can't easily be subdivided further. We add one intermediate step at 45% before render and keep 75% after.

---

## Change 7: `api/services/full_pipeline_worker.py` — Wire video_id

**Current:** `build_video()` is called in orchestrator which uses `self.db_video_id`.

**Change:** Ensure `video_id` is passed to `build_video()` (via orchestrator already from Change 4D above). The orchestrator already has `self.db_video_id`. Just need to pass it through.

Already covered in Change 4D. No additional changes.

For the worker, no changes needed since `_progress_to_db` already handles all progress callbacks.

---

## Change 8: `api/services/generation_service.py` — Sync milestones

No structural changes needed — the orchestrator progress_cb (`_progress_cb` lambda at line 1325-1329) already calls `_broadcast_progress()` which sends to WebSocket. Since all new progress is emitted through the orchestrator's `_emit_progress()` → `self._progress_cb()` chain, legacy mode benefits automatically.

Just verify that `db.update_video()` calls at phase boundaries don't overwrite the granular progress — they're final-phase values (12, 25, 40, 55, 75, 78, 85, 87, 90, 100) so no conflict.

---

## Change 9: `pipeline/shorts_tts.py` — Pass progress_cb through

**Location:** `synthesize_shorts_blocks()` at line 284

**Change:** Add `progress_cb` parameter and pass it to `generate_segmented()`:

```python
# At line 284, modify signature:
def synthesize_shorts_blocks(
    bloques: list[dict],
    ch_config,
    output_audio_path: Path,
    output_srt_path: Path,
    voice: Optional[str] = None,
    max_duration_sec: float = SHORTS_MAX_DURATION_SEC,
    progress_cb: callable = None,              # <-- NEW
) -> dict:

# For Kokoro path (line 327):
            audio_path_str, all_timestamps = engine.generate_segmented(
                bloques, output_path=str(output_audio_path),
                progress_cb=progress_cb,
            )

# For edge-tts path: edge-tts does single shot, no per-block. Skip.
```

---

## Summary of new progress percentages

### Long-form videos (after changes)

| % | Phase | What | Before |
|---|---|---|---|
| 1 | inicio | Job started | ✓ same |
| 5 | scrape | Starting scrape | ✓ same |
| 6-9 | scrape | Per-scraper stagger (was all 7%) | **NEW** |
| 10 | scrape | Scrape complete | ✓ same |
| 12 | scrape | Phase done | ✓ same |
| 15-23 | script | LLM batch (already granular) | ✓ same |
| 24 | script | SEO enrichment | ✓ same |
| 25 | script | Phase done | ✓ same |
| 27 | pre_validate | Validation | ✓ same |
| 30 | tts | Starting TTS | ✓ same |
| 31-37 | tts | Per-block synthesis (was invisible) | **NEW** |
| 38 | tts | Audio done | ✓ same |
| 39 | tts | CTA audio | ✓ same |
| 40 | tts | Phase done | ✓ same |
| 42-45 | media | Starting media fetch | ✓ same |
| 46-52 | media | Per-scene fetch (was invisible) | **NEW** |
| 53 | media | Assets ready | ✓ same |
| 55 | media | Phase done | ✓ same |
| 60-67 | video | Segment rendering (already exists via DB) | routed to cb |
| 69 | video | Starting xfade concat | **NEW** |
| 71 | video | Body assembled, adding CTA | **NEW** |
| 73 | video | Intro/outro + final montage | **NEW** |
| 74 | video | Audio post-process | **NEW** |
| 75 | video | Phase done | ✓ same |
| 78-100 | metadata+upload | Same as before | ✓ same |

### Native shorts (after changes)

| % | Phase | Before |
|---|---|---|
| 10 | script | ✓ same |
| 15-22 | tts | Per-block (was invisible) **NEW** |
| 25 | tts | ✓ same |
| 28-48 | media | Per-scene fetch (was invisible) **NEW** |
| 50 | media | ✓ same |
| 55-73 | render | Render steps (was invisible) **NEW** |
| 75 | render | ✓ same |
| 90 | upload | ✓ same |

### Clip shorts (after changes)

| % | Phase | Before |
|---|---|---|
| 10 | script | ✓ same |
| 20 | script | ✓ same |
| 30 | media | ✓ same |
| 45 | render | Starting render **NEW** |
| 75 | render | ✓ same |
| 90 | upload | ✓ same |

---

## Testing

After implementation:
```bash
# Quick test — observe progress in API logs
python3 test_video.py --canal canal2 --skip-scrape --quick

# Or via API: start a short and watch progress via WebSocket
# Check that progress never stays frozen at the same % for >30s
```
