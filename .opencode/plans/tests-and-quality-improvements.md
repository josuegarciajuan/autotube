# Plan: Tests + Upload + Test Video (1/4 duration)

## T1: Hacer que test mode use generate_v2 (en lugar del viejo generate)

**Archivo:** `pipeline/script_generator.py` línea 1657

**Cambio:** Reemplazar el bloque `if not test_mode` → v2 con:

```python
cfg = self.canal_config
test_mode = getattr(cfg, "TEST_MODE", False)

# Always use v2 sequential block-by-block generation (including outline-first).
# Test mode now uses v2 too — the word targets are already reduced via
# TEST_SCRIPT_WORDS_MIN/MAX and _compute_word_target().
palabras_obj = content_item.get("_palabras_objetivo", None)
return self.generate_v2(content_item, palabras_objetivo=palabras_obj)
```

Esto elimina las ~30 líneas siguientes del old path (multi-chunk/single-chunk legacy).

---

## T2: Añadir flag --quarter a test_video.py

**Archivo:** `test_video.py`

**Cambio:** Añadir argumento `--quarter` al parser y lógica para fijar duración a 1/4:

```python
# En argparse (cerca de línea 200):
parser.add_argument("--quarter", action="store_true",
                   help="Generate video at 1/4 normal duration (~3-4 min)")

# En la lógica de modo (después de las líneas 217-240):
quarter_dur = cfg.VIDEO_AVERAGE_DURATION_MIN / 4.0

if args.prod:
    cfg.TEST_MODE = False
    if hasattr(cfg, '_quick_images_override'):
        delattr(cfg, '_quick_images_override')
elif args.quarter:
    cfg.TEST_MODE = True
    # 1/4 duration: compute word targets proportionally
    quarter_words = max(300, int(cfg.PROD_SCRIPT_WORDS_MIN / 4))
    cfg.TEST_SCRIPT_WORDS_MIN = quarter_words
    cfg.TEST_SCRIPT_WORDS_MAX = quarter_words + 300
    cfg.TEST_SCRIPT_SCENES_MIN = max(4, cfg.PROD_SCRIPT_SCENES_MIN // 3)
    cfg.TEST_SCRIPT_SCENES_MAX = max(6, cfg.PROD_SCRIPT_SCENES_MAX // 3)
    cfg.TEST_SCRIPT_BLOCKS_MIN = max(3, cfg.PROD_SCRIPT_BLOCKS_MIN // 3)
    cfg.TEST_SCRIPT_BLOCKS_MAX = max(5, cfg.PROD_SCRIPT_BLOCKS_MAX // 3)
    cfg.TEST_VIDEO_DURATION_TARGET = quarter_dur
elif args.quick:
    cfg.TEST_MODE = True
    ... (existing quick logic unchanged)
else:
    cfg.TEST_MODE = True
    ... (existing default test logic unchanged)
```

---

## T3: Escribir tests unitarios

### T3a: `tests/test_content_prompt.py` — test de build_outline_prompt

```python
class TestOutlinePrompt:
    def test_outline_prompt_demands_concrete_facts(self):
        prompt = build_outline_prompt(config=MockConfigCanal2, duration_min=15, word_target=2500)
        assert "HECHOS CONCRETOS" in prompt
        assert "PROHIBIDO" in prompt
        assert "metáforas vacías" in prompt

    def test_outline_prompt_has_chapter_structure(self):
        prompt = build_outline_prompt(config=MockConfigCanal2, duration_min=12, word_target=2000)
        assert '"chapters"' in prompt
        assert '"titulo"' in prompt
        assert '"idea_central"' in prompt

    def test_outline_prompt_includes_visual_keywords(self):
        prompt = build_outline_prompt(config=MockConfigCanal2, duration_min=10, word_target=1500)
        assert "visual_keywords_en" in prompt
        assert "stock media" in prompt.lower()

    def test_content_prompt_accepts_outline_params(self):
        # Verify new params don't break existing behavior
        prompt = build_content_only_prompt(
            config=MockConfigCanal2,
            outline={"chapters": [{"titulo": "Intro", "idea_central": "x", "hechos_concretos": ["f1"], "visual_keywords_en": "test", "emocion_objetivo": "asombro"}]},
            batch_num=1,
        )
        assert "CONTEXTO DEL CAPÍTULO" in prompt
        assert "Intro" in prompt
```

### T3b: `tests/test_block_batch.py` — test de outline passing

```python
class TestOutlinePassing:
    def test_passes_outline_to_prompt_builder(self):
        sg = self._make_sg()
        sg.client.chat.completions.create.return_value = \
            make_mock_openai_response({"bloques": [{"texto": "Test block"}]})

        with patch("pipeline.script_generator.importlib.import_module") as mock_import:
            mock_prompts = MagicMock()
            mock_prompts.build_content_only_prompt.return_value = "prompt with outline"
            mock_import.return_value = mock_prompts

            outline = {"chapters": [{"titulo": "Cap 1"}]}
            sg._generate_blocks_batch(
                {"id": 1, "title": "Test"}, None, 250, "source",
                outline=outline, batch_num=2,
            )

            call_kwargs = mock_prompts.build_content_only_prompt.call_args[1]
            assert call_kwargs.get("outline") == outline
            assert call_kwargs.get("batch_num") == 2
```

### T3c: `tests/test_generate_v2.py` — test de outline generation

```python
class TestV2WithOutline:
    def test_outline_called_before_blocks(self):
        sg = self._make_sg()
        sg._generate_outline = MagicMock(return_value={"chapters": [{"titulo": "T"}]})
        self._mock_batches(sg, [["test block content"]], 500)
        sg._save_and_return = lambda **kw: {"id": 999, "guion": "test"}
        
        item = {"id": 1, "title": "Test", "text": "source text for outline"}
        result = sg.generate_v2(item, palabras_objetivo=500)
        
        assert sg._generate_outline.called
        assert result is not None

    def test_catches_outline_failure_gracefully(self):
        sg = self._make_sg()
        sg._generate_outline = MagicMock(side_effect=Exception("LLM error"))
        self._mock_batches(sg, [["test block content"]], 500)
        sg._save_and_return = lambda **kw: {"id": 999, "guion": "test"}
        
        item = {"id": 1, "title": "Test", "text": "source"}
        result = sg.generate_v2(item, palabras_objetivo=500)
        
        # Should continue without outline if outline fails
        assert result is not None
```

### T3d: `tests/test_video_editor.py` — tests de Ken Burns en video

```python
class TestVideoKenBurns:
    @patch("pipeline.video_editor.VideoFileClip")
    def test_video_clip_applies_zoom(self, mock_vfc):
        mock_clip = MagicMock()
        mock_clip.w = 1920
        mock_clip.h = 1080
        mock_clip.duration.return_value = 30.0
        mock_clip.duration = 30.0
        mock_clip.resized.return_value = mock_clip
        mock_clip.subclipped.return_value = mock_clip
        mock_vfc.return_value = mock_clip

        from pipeline.video_editor import VideoEditor
        ve = VideoEditor(_make_mock_config())
        clip = ve._video_clip_for_block(Path("/tmp/test.mp4"), 8.0)
        
        assert clip is not None
        # Should call resized at least 3 times (cap, zoom lambda, final crop)

class TestKenBurnsPan:
    def test_pan_factor_is_1(self):
        from pipeline.video_editor import VideoEditor
        import numpy as np
        from PIL import Image
        import tempfile
        
        ve = VideoEditor(_make_mock_config())
        img = Image.new("RGB", (2560, 1440), (128, 128, 128))
        with tempfile.NamedTemporaryFile(suffix=".jpg") as f:
            img.save(f.name)
            clip = ve._single_ken_burns_clip(Path(f.name), 4.0, 5.0)
            frame0 = clip.get_frame(0)
            frame_end = clip.get_frame(3.99)
            # Frames should differ (pan active) due to factor 1.0
            assert not np.array_equal(frame0, frame_end)
```

### T3e: `tests/test_media_fetcher.py` — tests de validación y video forzado

```python
class TestVideoValidation:
    def test_is_valid_video_uses_ffprobe(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "10.5"
            with tempfile.NamedTemporaryFile(suffix=".mp4") as f:
                f.write(b'\x00\x00\x00\x18ftypisom' + b'\x00' * 50000)
                f.flush()
                result = MediaFetcher._is_valid_video(Path(f.name))
                assert result is True
                mock_run.assert_called_once()

    def test_is_valid_video_rejects_corrupt(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stderr = "Invalid data found"
            with tempfile.NamedTemporaryFile(suffix=".mp4") as f:
                f.write(b'\x00\x00\x00\x18ftypisom' + b'\x00' * 50000)
                f.flush()
                result = MediaFetcher._is_valid_video(Path(f.name))
                assert result is False

class TestVideoForcing:
    def test_hook_and_climax_forced_video(self):
        from pipeline.media_fetcher import MediaFetcher
        mf = MediaFetcher(_make_config())
        scenes = [
            _make_scene_range(tipo="hook", duration=6),
            _make_scene_range(tipo="desarrollo", duration=8),
            _make_scene_range(tipo="climax", duration=10),
            _make_scene_range(tipo="desarrollo", duration=7),
            _make_scene_range(tipo="reflexion", duration=5),
        ]
        # Call fetch_for_script and check that hook + climax are in forced_video
        # ... (mock providers to track assignments)
```

### T3f: `tests/test_orchestrator.py` — test de limpieza de disco

```python
class TestDiskCleanup:
    @patch("orchestrator.shutil.rmtree")
    @patch("orchestrator.Path")
    def test_cleans_output_dirs_before_render(self, mock_path, mock_rmtree):
        from orchestrator import PipelineOrchestrator
        # Setup orchestrator with mocked DB
        # Call phase_video and verify shutil.rmtree was called for video_clips and temp
```

---

## T4: Ejecutar tests

```bash
python3 -m pytest tests/ -v --tb=short
```

---

## T5: Subir video existente

```bash
python3 main.py upload --canal canal2
```

Si el upload automático no detecta el video con status error, usar:
```bash
python3 main.py upload --canal canal2 --video output/videos/narration_kokoro_1782952067.mp4
```

---

## T6: Generar video de prueba a 1/4 duración

```bash
python3 test_video.py --canal canal2 --skip-scrape --quarter
```

Esto genera un video de ~3.75 minutos usando v2 + outline-first + todos los cambios de configuración (TTS más lento, escenas más cortas, Ken Burns mejorado, más mini-videos).
