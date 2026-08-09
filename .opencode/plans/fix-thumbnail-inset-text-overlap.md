# Fix: Thumbnail Inset Text Overlapping

## Problem
The text in the bottom-left (and bottom-right) inset boxes on thumbnails has words written over each other, making them unreadable.

## Root Cause (in `pipeline/thumbnail_maker.py:_draw_insets()`)

Two bugs in the text positioning logic:

### Bug 1: Double-counted line spacing
`_fit_text_to_box()` returns `total_h = line_h * len(lines)` where `line_h = int(asc * 1.15)` — the 15% spacing is already baked in. Then the drawing loop adds `line_spacing` AGAIN:
```python
start_y2 += fitted["total_h"] // len(fitted["lines"]) + fitted.get("line_spacing", 2)
```
This = `line_h + line_spacing` = `asc*1.15 + asc*0.15` = `asc*1.30`, double-counting the spacing.

### Bug 2: Incomplete height calculation (the actual clipping bug)
`total_h` uses only the **ascender** (from `textbbox` of "Ag"), ignoring the **descender**. When centering with `(inset_h - total_h) // 2`, the text block is positioned too high, causing the first line's ascender to extend ABOVE the overlay boundary (y=0), getting clipped.

Example: font_size=51, asc≈38, 2-line text:
- `line_h = int(38 * 1.15) = 43`
- `total_h = 43 * 2 = 86`
- `start_y2 = (144 - 86) // 2 = 29`
- First line top: `29 - 38 = -9` → **clipped 9px above the overlay!**

The clipped top of characters makes text appear garbled/overlapping.

## Fix

Replace the text positioning in both inset A (lines 1044-1056) and inset B (lines 1085-1096) to use actual `textbbox` measurements per line instead of the estimated `total_h` + `line_spacing` math.

### Inset A (bottom-left) — replace lines 1044-1056:

Replace:
```python
                # Centre all lines vertically
                total_h = fitted["total_h"]
                start_y2 = (inset_h - total_h) // 2
                for line in fitted["lines"]:
                    w_line, _ = self._measure_text_size(inset_draw_a, line, fitted["font"])
                    tx_a = (inset_w - w_line) // 2
                    inset_draw_a.text(
                        (tx_a, start_y2),
                        line,
                        fill=(220, 220, 220, 255),
                        font=fitted["font"],
                    )
                    start_y2 += fitted["total_h"] // len(fitted["lines"]) + fitted.get("line_spacing", 2)
```

With:
```python
                # Centre all lines vertically using actual line bounding boxes
                lines = fitted["lines"]
                line_bboxes = [inset_draw_a.textbbox((0, 0), l, font=fitted["font"]) for l in lines]
                ascents = [abs(b[1]) for b in line_bboxes]
                descents = [b[3] for b in line_bboxes]
                gap = max(2, int(fitted["font_size"] * 0.18))
                total_block_h = sum(ascents) + sum(descents) + gap * (len(lines) - 1)
                current_y = (inset_h - total_block_h) // 2 + ascents[0]
                for i, line in enumerate(lines):
                    w_line = line_bboxes[i][2] - line_bboxes[i][0]
                    tx_a = (inset_w - w_line) // 2
                    inset_draw_a.text(
                        (tx_a, current_y),
                        line,
                        fill=(220, 220, 220, 255),
                        font=fitted["font"],
                    )
                    if i < len(lines) - 1:
                        current_y += descents[i] + gap + ascents[i + 1]
```

### Inset B (bottom-right) — replace lines 1085-1096:

Replace:
```python
            total_h_b = fitted_b["total_h"]
            start_b = (inset_h2 - total_h_b) // 2
            for line in fitted_b["lines"]:
                w_line_b, _ = self._measure_text_size(inset_draw_b, line, fitted_b["font"])
                tx_b = (inset_w2 - w_line_b) // 2
                inset_draw_b.text(
                    (tx_b, start_b),
                    line,
                    fill=(200, 200, 200, 255),
                    font=fitted_b["font"],
                )
                start_b += total_h_b // len(fitted_b["lines"]) + fitted_b.get("line_spacing", 1)
```

With:
```python
            lines_b = fitted_b["lines"]
            line_bboxes_b = [inset_draw_b.textbbox((0, 0), l, font=fitted_b["font"]) for l in lines_b]
            ascents_b = [abs(b[1]) for b in line_bboxes_b]
            descents_b = [b[3] for b in line_bboxes_b]
            gap_b = max(2, int(fitted_b["font_size"] * 0.18))
            total_block_h_b = sum(ascents_b) + sum(descents_b) + gap_b * (len(lines_b) - 1)
            current_b = (inset_h2 - total_block_h_b) // 2 + ascents_b[0]
            for i, line in enumerate(lines_b):
                w_line_b = line_bboxes_b[i][2] - line_bboxes_b[i][0]
                tx_b = (inset_w2 - w_line_b) // 2
                inset_draw_b.text(
                    (tx_b, current_b),
                    line,
                    fill=(200, 200, 200, 255),
                    font=fitted_b["font"],
                )
                if i < len(lines_b) - 1:
                    current_b += descents_b[i] + gap_b + ascents_b[i + 1]
```

## Verification
After applying the fix:
1. Run a test thumbnail generation: `python3 test_video.py --canal canal2 --skip-scrape --quick`
2. Check the output thumbnail for readable inset text
3. Or use the isolated thumbnail maker: `python3 scripts/make_thumbnail.py --video-id <id>`
