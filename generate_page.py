#!/usr/bin/env python3
"""Generate the Autotube web dashboard page."""
import sys, json, sqlite3, shutil
sys.path.insert(0, '/root/autotube')
from pathlib import Path
from datetime import datetime
from config.settings import DATABASE_PATH, OUTPUT_DIR

WEB_DIR = Path('/var/www/html/atupuerta/autotube')
VIDEO_WEB_DIR = WEB_DIR / 'videos'

# Copy latest video if exists
video_files = sorted(Path('output/videos').glob('*.mp4'))
for vf in video_files:
    dest = VIDEO_WEB_DIR / vf.name
    if not dest.exists():
        shutil.copy2(vf, dest)

# Check background build status
bg_log = Path('/tmp/build_video.log')
bg_status = bg_log.read_text()[-500:] if bg_log.exists() else "Waiting..."

# DB stats
conn = sqlite3.connect(str(DATABASE_PATH))
conn.row_factory = sqlite3.Row

total_content = conn.execute("SELECT COUNT(*) as c FROM raw_content WHERE canal='canal2'").fetchone()['c']
total_scripts = conn.execute("SELECT COUNT(*) as c FROM scripts WHERE canal='canal2'").fetchone()['c']
total_used = conn.execute("SELECT COUNT(*) as c FROM scripts WHERE canal='canal2' AND used=1").fetchone()['c']

# Latest script
latest = conn.execute("SELECT * FROM scripts WHERE canal='canal2' ORDER BY created_at DESC LIMIT 1").fetchone()
latest_dict = dict(latest) if latest else None
if latest_dict:
    titles = json.loads(latest_dict['titulo_options'])
    escenas = json.loads(latest_dict['escenas_json'])
    keywords = json.loads(latest_dict['keywords_json'])

# Pipeline log
logs = conn.execute(
    "SELECT * FROM pipeline_log WHERE canal='canal2' ORDER BY created_at DESC LIMIT 20"
).fetchall()

# Source content
sources = conn.execute(
    "SELECT * FROM raw_content WHERE canal='canal2' ORDER BY scraped_at DESC LIMIT 5"
).fetchall()

conn.close()

# Video list
videos = sorted(VIDEO_WEB_DIR.glob('*.mp4'))

# Escape for HTML
def esc(s):
    return str(s).replace('&','&amp;').replace('<','&lt;').replace('>','&gt;')

html = f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Autotube — Sincronías</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#0d0d0d;color:#e0e0e0;line-height:1.6}}
header{{background:linear-gradient(135deg,#1a1a2e,#16213e);padding:2rem;text-align:center;border-bottom:3px solid #b41e1e}}
header h1{{font-size:2rem;color:#e63946}}
header p{{color:#aaa;margin-top:.5rem}}
.container{{max-width:1200px;margin:0 auto;padding:2rem}}
.card{{background:#1a1a2e;border-radius:12px;padding:1.5rem;margin-bottom:1.5rem;border:1px solid #2a2a4a}}
.card h2{{color:#e63946;margin-bottom:1rem;font-size:1.3rem;border-bottom:1px solid #2a2a4a;padding-bottom:.5rem}}
.video-container{{position:relative;padding-bottom:56.25%;height:0;overflow:hidden;border-radius:8px;background:#000}}
.video-container video,.video-container iframe{{position:absolute;top:0;left:0;width:100%;height:100%}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:1rem}}
.stat{{background:#16213e;padding:1rem;border-radius:8px;text-align:center}}
.stat .num{{font-size:2rem;color:#e63946;font-weight:bold}}
.stat .label{{color:#888;font-size:.85rem}}
table{{width:100%;border-collapse:collapse;font-size:.9rem}}
th,td{{padding:.5rem .8rem;text-align:left;border-bottom:1px solid #2a2a4a}}
th{{color:#e63946;font-weight:600}}
tr:hover{{background:#16213e}}
.status-ok{{color:#2ecc71}}
.status-err{{color:#e74c3c}}
.status-skip{{color:#f39c12}}
.tag{{display:inline-block;background:#16213e;color:#aaa;padding:.2rem .6rem;border-radius:4px;margin:.2rem;font-size:.8rem}}
pre{{background:#0d0d0d;padding:1rem;border-radius:8px;overflow-x:auto;font-size:.8rem;max-height:300px;overflow-y:auto}}
.log-line{{font-family:monospace;font-size:.75rem;padding:.1rem 0;border-bottom:1px solid #1a1a2e}}
footer{{text-align:center;padding:2rem;color:#555;font-size:.8rem}}
.process-steps{{display:flex;flex-wrap:wrap;gap:.5rem;margin:1rem 0}}
.step{{flex:1;min-width:100px;background:#16213e;padding:.8rem;border-radius:8px;text-align:center;font-size:.8rem;position:relative}}
.step .step-num{{font-size:1.5rem;font-weight:bold;color:#e63946}}
.step.active{{border:2px solid #e63946}}
.step.done{{opacity:.7}}
.step.done::after{{content:'✓';color:#2ecc71;position:absolute;top:5px;right:8px;font-weight:bold}}
</style>
</head>
<body>
<header>
<h1>🎬 Autotube — Canal 1</h1>
<p>Historias Impactantes Reales · Pipeline Automatizado · {datetime.now().strftime('%d/%m/%Y %H:%M')}</p>
</header>

<div class="container">

<!-- VIDEO SECTION -->
<div class="card">
<h2>▶ Video Generado</h2>
"""
# Add video if exists
if videos:
    latest_video = videos[-1]
    html += f"""
<div class="video-container">
<video controls poster="{latest_video.stem}.png">
  <source src="{latest_video.name}" type="video/mp4">
  Tu navegador no soporta video.
</video>
</div>
<p style="margin-top:1rem;color:#888;font-size:.85rem">
  Archivo: {latest_video.name} · {latest_video.stat().st_size/1024/1024:.1f} MB
</p>"""
else:
    html += """
<p style="color:#f39c12">⏳ Renderizando video en segundo plano...</p>
<div style="background:#0d0d0d;padding:1rem;border-radius:8px;max-height:200px;overflow-y:auto;margin-top:1rem">
<p style="color:#888;font-size:.8rem">Log de renderizado:</p>"""
    for line in bg_status.split('\n')[-15:]:
        html += f'<div class="log-line">{esc(line)}</div>'
    html += '</div>'

html += """
</div>

<!-- STATS -->
<div class="card">
<h2>📊 Estadísticas del Pipeline</h2>
<div class="grid">
<div class="stat"><div class="num">""" + str(total_content) + """</div><div class="label">Contenido scrapeado</div></div>
<div class="stat"><div class="num">""" + str(total_scripts) + """</div><div class="label">Guiones generados</div></div>
<div class="stat"><div class="num">""" + str(total_used) + """</div><div class="label">Guiones usados</div></div>
<div class="stat"><div class="num">""" + str(len(videos)) + """</div><div class="label">Videos renderizados</div></div>
</div>
"""

# Process flow
html += """
<h2 style="margin-top:1.5rem">⚙️ Flujo del Pipeline</h2>
<div class="process-steps">
<div class="step done"><div class="step-num">1</div>Scraping<br><small>Reddit + Wikipedia</small></div>
<div class="step done"><div class="step-num">2</div>GPT Script<br><small>DeepSeek / OpenAI</small></div>
<div class="step done"><div class="step-num">3</div>TTS Voz<br><small>Edge TTS</small></div>
<div class="step done"><div class="step-num">4</div>Imágenes<br><small>Unsplash + Pexels</small></div>
<div class="step active"><div class="step-num">5</div>Video<br><small>MoviePy</small></div>
<div class="step"><div class="step-num">6</div>Upload<br><small>YouTube API</small></div>
</div>
"""

# Latest script info
if latest_dict:
    titulo = titles[0] if titles else "Sin título"
    html += f"""
</div>

<!-- LATEST SCRIPT -->
<div class="card">
<h2>📝 Último Guion Generado</h2>
<p><strong>Título:</strong> {esc(titulo)}</p>
<p><strong>Alternativas:</strong> {esc(' · '.join(titles[1:]))}</p>
<p><strong>Escenas:</strong> {len(escenas)} · <strong>Keywords:</strong> {esc(', '.join(keywords[:10]))}</p>
<p><strong>Duración estimada:</strong> {latest_dict['duracion_estimada']} min · <strong>Tokens:</strong> {latest_dict['token_count']} · <strong>Coste:</strong> ${latest_dict['cost_estimate']:.4f}</p>
<details style="margin-top:1rem">
<summary style="cursor:pointer;color:#e63946">Ver guion completo ({len(latest_dict['guion'])} caracteres)</summary>
<pre>{esc(latest_dict['guion'][:3000])}{'...' if len(latest_dict['guion'])>3000 else ''}</pre>
</details>
"""

# Source content
html += """
</div>

<!-- SOURCE CONTENT -->
<div class="card">
<h2>📡 Fuentes de Contenido</h2>
<table>
<tr><th>Fuente</th><th>Subreddit</th><th>Título</th><th>Score</th></tr>
"""
for s in sources:
    sd = dict(s)
    html += f"<tr><td>{esc(sd['source'])}</td><td>{esc(sd.get('subreddit','-'))}</td><td>{esc(sd['title'][:60])}</td><td>{sd['score']}</td></tr>\n"
html += "</table>"

# Tags
if latest_dict:
    html += '<div style="margin-top:1rem">'
    for kw in keywords[:15]:
        html += f'<span class="tag">{esc(kw)}</span> '
    html += '</div>'

html += """
</div>

<!-- PIPELINE LOG -->
<div class="card">
<h2>📋 Log del Pipeline</h2>
<table>
<tr><th>Hora</th><th>Fase</th><th>Estado</th><th>Mensaje</th></tr>
"""
for row in logs:
    r = dict(row)
    status_class = 'status-ok' if r['status']=='success' else 'status-err' if r['status']=='error' else 'status-skip'
    status_text = '✓' if r['status']=='success' else '✗' if r['status']=='error' else '○'
    html += f"<tr><td>{r['created_at'][11:19]}</td><td>{r['phase']}</td><td class='{status_class}'>{status_text}</td><td>{esc(str(r['message'] or '')[:80])}</td></tr>\n"
html += """
</table>
</div>

</div><!-- container -->

<footer>
<p>Autotube v1.0 · Pipeline automatizado de YouTube · Coste operativo ~$11/mes · 100% contenido original</p>
<p>Servido desde lamami.online · {timestamp}</p>
</footer>

<script>
// Auto-refresh page every 30 seconds if video is being built
if (!document.querySelector('video')) {{
    setTimeout(function(){{ location.reload(); }}, 30000);
}}
</script>
</body>
</html>""".format(timestamp=datetime.now().strftime('%d/%m/%Y %H:%M:%S'))

# Write page
index_path = WEB_DIR / 'index.html'
index_path.write_text(html, encoding='utf-8')
print(f"Page written: {index_path}")
print(f"URL: https://lamami.online/autotube/")
