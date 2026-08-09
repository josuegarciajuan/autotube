# Plan: Mejorar fechas en listado de videos por canal

## Problema
En `frontend/src/pages/ChannelDetail.tsx:1585`, la fecha se muestra sin contexto:
```tsx
<span>{formatDateTime(v.uploaded_at || v.created_at)}</span>
```
- No se sabe si es fecha de subida, creación o publicación
- No distingue visualmente entre un video subido/no subido/público
- Ignora `published_at`: un video público muestra fecha de subida en vez de publicación

## Cambios

### 1. Añadir import de `CheckCircle` (línea 6)
```
import { ..., Share2, CheckCircle } from 'lucide-react'
```

### 2. Reemplazar la fila de fecha+links (líneas 1584-1606)

**Antes:**
```tsx
<div className="flex items-center gap-1.5 mt-0.5 text-xs text-gray-600">
  <span>{formatDateTime(v.uploaded_at || v.created_at)}</span>
  {v.target_playlist_name && (...)}
  ...
</div>
```

**Después:**
```tsx
{/* ── Status + date row ── */}
{(() => {
  const isPublished = !!(v.published_at) || v.status === 'published';
  const isUploaded = !isPublished && (!!(v.yt_video_id) || !!(v.uploaded_at));
  const displayDate = (v.published_verified_at || v.published_at) ||
    v.uploaded_at ||
    v.generation_started_at || v.created_at;
  const dateLabel = isPublished ? 'Publicado' : isUploaded ? 'Subido' : 'Creado';
  const DateIcon = isPublished ? CheckCircle : isUploaded ? Upload : Clock;
  const dateColor = isPublished ? 'text-emerald-400' : isUploaded ? 'text-blue-400' : 'text-gray-500';
  const dateBg = isPublished ? 'bg-emerald-400/10' : isUploaded ? 'bg-blue-400/10' : 'bg-gray-500/10';
  return (
    <div className="flex items-center gap-1 mt-0.5 flex-wrap min-w-0">
      <span className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-medium ${dateBg} ${dateColor}`}>
        <DateIcon size={10} />
        <span>{dateLabel}</span>
        <span className="text-[9px] opacity-70">{formatDateTime(displayDate)}</span>
      </span>
    </div>
  );
})()}
{/* ── Links row (playlist, YT, source, short) ── */}
<div className="flex items-center gap-1.5 mt-1 text-xs text-gray-600">
  {v.target_playlist_name && (...)}
  {v.yt_url && (...)}
  {v.source_url && (...)}
  {v.script_id && (...)}
</div>
```

### 3. Bonus: Mejorar badge de estado en la thumbnail (línea 1570-1572)

**Antes:**
```tsx
<span className={`absolute top-1.5 left-1.5 text-[10px] px-1.5 py-0.5 rounded font-medium badge ${statusBadge(displayStatus)}`}>
  {statusLabel(displayStatus)}
</span>
```

**Después:**
```tsx
<span className={`absolute top-1.5 left-1.5 text-[10px] px-1.5 py-0.5 rounded font-medium badge flex items-center gap-0.5 ${statusBadge(displayStatus)}`}>
  {displayStatus === 'published' && <CheckCircle size={9} />}
  {displayStatus === 'uploaded' && <Upload size={9} />}
  {displayStatus === 'uploaded_private' && <Upload size={9} />}
  {displayStatus === 'scheduled' && <Calendar size={9} />}
  {displayStatus === 'warming' && <Clock size={9} />}
  {displayStatus === 'generating' && <Loader2 size={9} className="animate-spin" />}
  {displayStatus === 'error' && <AlertCircle size={9} />}
  {statusLabel(displayStatus)}
</span>
```

### 4. Misma mejora en `RecentVideos.tsx` (opcional)
En la vista de dashboard "Flujo de videos" (línea 132-136), donde se muestra "Sin acciones registradas" cuando no hay `lastAction`, se podría añadir también un pill similar con la fecha de publicación/subida/creación.
