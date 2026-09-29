'use strict';

/**
 * unlock UI — 3x3 Android-style pattern lock (mouse + touch via Pointer Events).
 * The pattern is only ever verified server-side; this file just captures it and
 * POSTs {pattern:"0-1-2-5-8"} to /__gate/unlock, then reloads.
 */
(function () {
  var canvas = document.getElementById('pad');
  if (!canvas || !canvas.getContext) return;
  var ctx = canvas.getContext('2d');
  var msg = document.getElementById('msg');

  var SIZE = canvas.width; // 300
  var N = 3;
  var CELL = SIZE / N;
  var DOT_R = 16;
  var HIT_R = 30;
  var LINE_W = 6;
  var min = 6;

  var dots = [];
  for (var row = 0; row < N; row++) {
    for (var col = 0; col < N; col++) {
      dots.push({ i: row * N + col, x: col * CELL + CELL / 2, y: row * CELL + CELL / 2 });
    }
  }

  var sequence = [];
  var drawing = false;
  var cursor = null;

  // Pull runtime config (keeps UI min in sync with PATTERN_MIN on the server).
  fetch('/__gate/config', { credentials: 'same-origin' })
    .then(function (r) {
      return r.ok ? r.json() : null;
    })
    .then(function (cfg) {
      if (cfg && typeof cfg.min === 'number' && cfg.min > 0) min = cfg.min;
    })
    .catch(function () {});

  function setMsg(text, isError) {
    msg.textContent = text || '';
    msg.classList.toggle('err', !!isError);
  }

  function toLocal(ev) {
    var rect = canvas.getBoundingClientRect();
    return {
      x: ((ev.clientX - rect.left) * SIZE) / rect.width,
      y: ((ev.clientY - rect.top) * SIZE) / rect.height,
    };
  }

  function hit(p) {
    for (var k = 0; k < dots.length; k++) {
      var d = dots[k];
      var dx = p.x - d.x;
      var dy = p.y - d.y;
      if (dx * dx + dy * dy <= HIT_R * HIT_R) return d;
    }
    return null;
  }

  function draw() {
    ctx.clearRect(0, 0, SIZE, SIZE);

    // connecting lines
    if (sequence.length) {
      ctx.strokeStyle = 'rgba(88, 166, 255, 0.85)';
      ctx.lineWidth = LINE_W;
      ctx.lineCap = 'round';
      ctx.lineJoin = 'round';
      ctx.beginPath();
      ctx.moveTo(dots[sequence[0]].x, dots[sequence[0]].y);
      for (var i = 1; i < sequence.length; i++) {
        ctx.lineTo(dots[sequence[i]].x, dots[sequence[i]].y);
      }
      if (drawing && cursor) ctx.lineTo(cursor.x, cursor.y);
      ctx.stroke();
    }

    // dots
    for (var k = 0; k < dots.length; k++) {
      var d = dots[k];
      var active = sequence.indexOf(d.i) !== -1;
      ctx.beginPath();
      ctx.arc(d.x, d.y, DOT_R, 0, Math.PI * 2);
      ctx.fillStyle = active ? '#58a6ff' : 'rgba(230, 237, 243, 0.10)';
      ctx.fill();
      ctx.lineWidth = 2;
      ctx.strokeStyle = active ? 'rgba(88, 166, 255, 0.95)' : 'rgba(230, 237, 243, 0.30)';
      ctx.stroke();
      if (active) {
        ctx.beginPath();
        ctx.arc(d.x, d.y, 5, 0, Math.PI * 2);
        ctx.fillStyle = '#0d1117';
        ctx.fill();
      }
    }
  }

  function reset() {
    sequence = [];
    cursor = null;
    drawing = false;
    draw();
  }

  function addDot(d) {
    if (!d) return;
    if (sequence.indexOf(d.i) !== -1) return;
    sequence.push(d.i);
    draw();
  }

  function submit(seq) {
    setMsg('Comprobando…', false);
    fetch('/__gate/unlock', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ pattern: seq.join('-') }),
    })
      .then(function (res) {
        return res
          .json()
          .catch(function () {
            return null;
          })
          .then(function (data) {
            return { status: res.status, data: data };
          });
      })
      .then(function (result) {
        if (result.status >= 200 && result.status < 300) {
          setMsg('Desbloqueado. Cargando…', false);
          window.location.reload();
          return;
        }
        if (result.status === 429) {
          var retry = (result.data && result.data.retryAfter) || 0;
          setMsg('Demasiados intentos. Espera ~' + Math.ceil(retry / 60) + ' min.', true);
        } else {
          setMsg((result.data && result.data.error) || 'Patrón incorrecto', true);
        }
        window.setTimeout(reset, 900);
      })
      .catch(function () {
        setMsg('Error de red. Reintenta.', true);
        window.setTimeout(reset, 900);
      });
  }

  function finish() {
    if (!drawing) return;
    drawing = false;
    cursor = null;
    draw();
    if (sequence.length >= min) {
      submit(sequence.slice());
    } else {
      setMsg('Conecta al menos ' + min + ' puntos', true);
      window.setTimeout(reset, 900);
    }
  }

  if (window.PointerEvent) {
    canvas.addEventListener('pointerdown', function (ev) {
      ev.preventDefault();
      try {
        canvas.setPointerCapture(ev.pointerId);
      } catch (_) {}
      reset();
      drawing = true;
      addDot(hit(toLocal(ev)));
    });
    canvas.addEventListener('pointermove', function (ev) {
      if (!drawing) return;
      ev.preventDefault();
      var p = toLocal(ev);
      var d = hit(p);
      if (d) addDot(d);
      else {
        cursor = p;
        draw();
      }
    });
    canvas.addEventListener('pointerup', finish);
    canvas.addEventListener('pointercancel', reset);
    canvas.addEventListener('pointerleave', function (ev) {
      if (!drawing) return;
      // keep drawing while capture is active; only clear cursor on real leave
      if (!canvas.hasPointerCapture || !canvas.hasPointerCapture(ev.pointerId)) {
        cursor = null;
        draw();
      }
    });
  } else {
    // Fallback for very old browsers: touch events.
    canvas.addEventListener(
      'touchstart',
      function (ev) {
        ev.preventDefault();
        reset();
        drawing = true;
        addDot(hit(toLocal(ev.touches[0])));
      },
      { passive: false }
    );
    canvas.addEventListener(
      'touchmove',
      function (ev) {
        ev.preventDefault();
        addDot(hit(toLocal(ev.touches[0])));
      },
      { passive: false }
    );
    canvas.addEventListener('touchend', function (ev) {
      ev.preventDefault();
      finish();
    });
  }

  draw();
})();
