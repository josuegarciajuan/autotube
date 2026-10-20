"""Asignador único de horarios de publicación (Fase 1).

Centraliza la regla de distribución que antes estaba repartida entre
``planning_service._distribute_slots``, el replan de ``optimal_slots_calculator``
y el planificador de shorts:

  * cada día reparte sus elementos entre las franjas del canal;
  * N <= F: N franjas distintas, elegidas por máxima separación (rank desempata);
  * N > F: todas las franjas y los extras comparten franja (solape secuencial);
  * el gap same-channel es preferencia, no veto: nunca manda al día siguiente;
  * el espaciado entre canales también es preferencia (si no hay franja libre,
    se permite el solape; nunca se cambia de día).

Todas las datetimes devueltas son *aware* en la zona horaria del canal.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo
from typing import Iterable, Sequence


@dataclass(frozen=True, order=True)
class Franja:
    """Franja de publicación diaria.

    ``rank`` es la prioridad (1 = mejor). El asignador selecciona las mejores
    ``n`` franjas de cada día y luego las ordena por hora para publicarlas.
    """

    hour: int
    minute: int
    rank: int = 1

    def __post_init__(self) -> None:
        if not (0 <= int(self.hour) <= 23):
            raise ValueError(f"franja con hora inválida: {self.hour}")
        if not (0 <= int(self.minute) <= 59):
            raise ValueError(f"franja con minuto inválido: {self.minute}")


def _unique_franjas(franjas: Iterable[Franja]) -> list[Franja]:
    """Deduplica por hora:minuto conservando el mejor (menor) rank."""
    best: dict[tuple[int, int], Franja] = {}
    for f in franjas:
        key = (int(f.hour), int(f.minute))
        current = best.get(key)
        if current is None or f.rank < current.rank:
            best[key] = Franja(key[0], key[1], int(f.rank))
    return list(best.values())


def select_daily_franjas(franjas: Sequence[Franja], count: int) -> list[Franja]:
    """Selecciona las ``count`` mejores franjas por rank y las ordena por hora.

    La selección por rank garantiza calidad (se usa el pico real); el orden
    cronológico garantiza que los slots de un mismo día se repartan a lo largo
    del día en vez de apilarse.
    """
    if count <= 0:
        return []
    unique = _unique_franjas(franjas)
    ordered = sorted(unique, key=lambda f: (f.rank, f.hour * 60 + f.minute))
    selected = ordered[:count]
    return sorted(selected, key=lambda f: (f.hour * 60 + f.minute, f.rank))


def _combine(day: date, franja: Franja, tz: tzinfo) -> datetime:
    return datetime.combine(
        day, time(int(franja.hour), int(franja.minute)), tzinfo=tz,
    )


def allocate_publish_times(
    franjas: Sequence[Franja],
    day_counts: Sequence[tuple[date, int]],
    *,
    tz: tzinfo,
    earliest: datetime | None = None,
    min_gap_minutes: int = 0,
    daily_cap: int | dict[date, int] | object = 0,
    busy: Sequence[datetime] | None = None,
    day_usage: dict[date, int] | None = None,
    max_extra_days: int = 366,
) -> list[datetime]:
    """Reparte ``day_counts`` publicaciones respetando cap, gap y ranking.

    Args:
        franjas: franjas candidatas (se deduplican y se ordenan por rank).
        day_counts: pares ``(día, nº de publicaciones deseadas)`` en orden.
        tz: zona horaria del canal. Las datetimes devueltas la llevan.
        earliest: instante mínimo (aware). Los slots anteriores se descartan y
            su hueco se arrastra al día siguiente.
        min_gap_minutes: separación mínima entre publicaciones consecutivas.
        daily_cap: máximo de publicaciones por día (0 = sin tope). Puede ser
            un entero, un dict ``{date: cap}`` o un callable ``f(date) -> int``
            para estrategias con cap variable por día (rotación).
        busy: publicaciones ya comprometidas (vídeos/slots running). El
            asignador no las mueve; simplemente las esquiva respetando el gap.
        day_usage: ocupación inicial por día local (para el cap diario).
        max_extra_days: tope de días extra para vaciar el arrastre.

    Returns:
        Lista ordenada de datetimes locales (aware).
    """
    if not franjas or not day_counts:
        return []

    gap_seconds = max(0, int(min_gap_minutes)) * 60
    gap = timedelta(seconds=gap_seconds)

    def _cap_for(day: date) -> int:
        if isinstance(daily_cap, dict):
            if day in daily_cap:
                return int(daily_cap[day] or 0)
            return max((int(v or 0) for v in daily_cap.values()), default=0)
        if callable(daily_cap):
            return int(daily_cap(day) or 0)
        return int(daily_cap or 0)
    busy_list = sorted(busy or [])
    used = dict(day_usage or {})
    out: list[datetime] = []
    last: datetime | None = None
    carry = 0
    index = 0
    extra_days = 0
    last_requested_day = day_counts[-1][0] if day_counts else None

    while index < len(day_counts) or carry > 0:
        if index < len(day_counts):
            day, requested = day_counts[index]
            index += 1
            requested = max(0, int(requested))
        else:
            if last_requested_day is None or extra_days >= max_extra_days:
                break
            extra_days += 1
            day = last_requested_day + timedelta(days=extra_days)
            requested = 0

        want = requested + carry
        if want <= 0:
            carry = 0
            continue

        cap_value = _cap_for(day)
        cap = cap_value if cap_value > 0 else want
        take = min(want, max(0, cap - used.get(day, 0)))
        if take <= 0:
            carry = want
            continue

        # Primary: las `take` mejores franjas. Fallback: el resto de franjas
        # usables, solo si una primaria está bloqueada por un target ocupado.
        primary = select_daily_franjas(franjas, take)
        fallback = select_daily_franjas(
            franjas, min(len(_unique_franjas(franjas)), max(take, cap)),
        )
        candidates = primary + [f for f in fallback if f not in primary]
        if not candidates:
            carry = want
            continue

        placed = 0
        for franja in candidates:
            if placed >= take:
                break
            candidate = _combine(day, franja, tz)
            if earliest is not None and candidate < earliest:
                continue
            if last is not None and candidate < last + gap:
                continue
            blocked = any(
                candidate == b
                or (gap_seconds > 0
                    and abs((candidate - b).total_seconds()) < gap_seconds)
                for b in busy_list
            )
            if blocked:
                continue
            out.append(candidate)
            last = candidate
            used[day] = used.get(day, 0) + 1
            placed += 1

        carry = want - placed

    out.sort()
    return out


def group_by_day(times: Iterable[datetime]) -> dict[date, list[datetime]]:
    """Agrupa datetimes por día local conservando el orden."""
    grouped: dict[date, list[datetime]] = {}
    for value in sorted(times):
        grouped.setdefault(value.date(), []).append(value)
    return grouped


# ── Franja-first allocation (oct 2026) ─────────────────────────────
# Regla de producto: cada día reparte sus elementos entre las franjas
# disponibles. Si hay menos o igual elementos que franjas, cada uno usa una
# franja distinta. Si hay más, los extras COMPARTEN franja (se publican
# seguidos) en vez de pasar al día siguiente. El gap same-channel es una
# preferencia, nunca un veto que cambie de día.


@dataclass(frozen=True)
class FranjaSlot:
    """Un elemento colocado en una franja concreta."""

    when: datetime
    franja: Franja
    duplicate_index: int = 0


def _cap_value(daily_cap, day: date) -> int:
    if isinstance(daily_cap, dict):
        if day in daily_cap:
            return int(daily_cap[day] or 0)
        return max((int(v or 0) for v in daily_cap.values()), default=0)
    if callable(daily_cap):
        return int(daily_cap(day) or 0)
    return int(daily_cap or 0)


def _minutes_between(a: datetime, b: datetime) -> float:
    return abs((a - b).total_seconds()) / 60.0


def _choose_franjas(
    available: Sequence[Franja],
    k: int,
    tz: tzinfo,
    day: date,
    busy: Sequence[datetime],
    soft_busy: bool,
    busy_spacing_minutes: int = 0,
) -> list[Franja]:
    """Elige ``k`` franjas maximizando separación (y evitando ``busy``)."""
    if k <= 0:
        return []
    ordered = sorted(available, key=lambda f: (f.rank, f.hour * 60 + f.minute))
    if k >= len(ordered):
        return ordered

    def _score(combo: Sequence[Franja]) -> tuple:
        times = [_combine(day, f, tz) for f in combo]
        own_sep = min(
            (_minutes_between(a, b) for i, a in enumerate(times)
             for b in times[i + 1:]),
            default=0.0,
        )
        busy_sep = min(
            (_minutes_between(t, b) for t in times for b in busy),
            default=float("inf"),
        )
        rank_score = -sum(f.rank for f in combo)
        if soft_busy:
            conflicts = sum(
                1 for t in times for b in busy
                if _minutes_between(t, b) < max(0, int(busy_spacing_minutes))
            )
            return (-conflicts, busy_sep, own_sep, rank_score)
        return (own_sep, rank_score)

    # F is small (3-6 in production). Guard the combinatorial explosion.
    if len(ordered) <= 12:
        combos = itertools.combinations(ordered, k)
    else:
        # Greedy: best rank first, then the farthest from the chosen ones.
        chosen = [ordered[0]]
        while len(chosen) < k:
            remaining = [f for f in ordered if f not in chosen]
            chosen.append(max(
                remaining,
                key=lambda f: min(
                    _minutes_between(_combine(day, f, tz),
                                     _combine(day, c, tz))
                    for c in chosen
                ),
            ))
        combos = [tuple(chosen)]

    best = max(combos, key=_score)
    return list(best)


def assign_franja_slots(
    franjas: Sequence[Franja],
    day_counts: Sequence[tuple[date, int]],
    *,
    tz: tzinfo,
    earliest: datetime | None = None,
    daily_cap: int | dict[date, int] | object = 0,
    busy: Sequence[datetime] | None = None,
    day_usage: dict[date, int] | None = None,
    soft_busy: bool = True,
    soft_gap: bool = True,
    overlap_offset_minutes: int = 3,
    busy_spacing_minutes: int = 0,
) -> list[FranjaSlot]:
    """Reparte cada día con la regla franja-first.

    - ``N <= F``: N franjas distintas, elegidas por máxima separación.
    - ``N > F``: todas las franjas; los extras comparten franja con un offset
      determinista (3 min, 6 min, ...) para reflejar la subida secuencial.
    - No hay ``carry`` al día siguiente por gap ni por falta de franjas. Cada
      día se resuelve con sus franjas; el horizonte ya incluye los días
      siguientes. Solo se recorta si ``daily_cap`` es menor que N (tope duro).
    - ``busy`` es una preferencia: si hay franjas libres se evitan; si todas
      están ocupadas, se permite el solape (nunca se cambia de día por esto).
    """
    if not franjas or not day_counts:
        return []

    unique = sorted(
        _unique_franjas(franjas),
        key=lambda f: (f.rank, f.hour * 60 + f.minute),
    )
    if not unique:
        return []

    used = dict(day_usage or {})
    busy_list = list(busy or [])
    out: list[FranjaSlot] = []
    offset = max(0, int(overlap_offset_minutes))

    for day, requested in day_counts:
        n = max(0, int(requested))
        if n <= 0:
            continue
        cap = _cap_value(daily_cap, day)
        if cap > 0:
            n = min(n, max(0, cap - used.get(day, 0)))
            if n <= 0:
                continue

        available = list(unique)
        if earliest is not None:
            available = [
                f for f in available
                if _combine(day, f, tz) >= earliest
            ]
        if not available:
            # El día ya no admite franjas (p. ej. hoy por la noche). No se
            # arrastra a otro día: el horizonte planifica cada fecha.
            continue

        k = min(n, len(available))
        selected = _choose_franjas(
            available, k, tz, day, busy_list, soft_busy=soft_busy,
            busy_spacing_minutes=busy_spacing_minutes,
        )
        # Extras: round-robin por rank (las mejores franjas reciben el extra).
        counts = {id(f): n // k for f in selected}
        remainder = n % k
        for f in sorted(selected, key=lambda x: (x.rank, x.hour * 60 + x.minute)):
            if remainder <= 0:
                break
            counts[id(f)] += 1
            remainder -= 1

        for franja in selected:
            count = counts.get(id(franja), 0)
            for dup_index in range(count):
                when = _combine(day, franja, tz) + timedelta(
                    minutes=offset * dup_index
                )
                out.append(FranjaSlot(when=when, franja=franja,
                                      duplicate_index=dup_index))
        used[day] = used.get(day, 0) + n

    out.sort(key=lambda s: (s.when, s.franja.rank))
    return out


def allocate_franja_times(*args, **kwargs) -> list[datetime]:
    """Wrapper de compatibilidad: solo devuelve las datetimes."""
    return [s.when for s in assign_franja_slots(*args, **kwargs)]
