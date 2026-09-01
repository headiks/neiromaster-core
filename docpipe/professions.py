"""
Сопоставление профессий, названных моделью (проход 2), с реальным штатным расписанием.
Требование: должности, которых нет в штатке, матчатся по эмбеддингу bge-m3; при косинусе
< 0.7 отбрасываются. Так метки профессий всегда указывают на существующие должности.
"""

from config import get_embedding

MATCH_THRESHOLD = 0.7
_vec_cache: dict = {}   # текст должности -> эмбеддинг (должностей немного)


def _vec(text: str):
    v = _vec_cache.get(text)
    if v is None:
        v = get_embedding(text)
        _vec_cache[text] = v
    return v


def _cos(a, b) -> float:
    if not a or not b:
        return 0.0
    s = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return s / (na * nb) if na and nb else 0.0


def staffing_positions() -> list:
    """Уникальные должности сотрудников из штатки (плейтекст). Ленивая зависимость от users."""
    import users
    seen = []
    for u in users.list_users():
        pos = (u.get("position") or "").strip()
        if pos and pos not in seen:
            seen.append(pos)
    return seen


def match_to_staffing(named: list, positions: list, threshold: float = MATCH_THRESHOLD,
                      embed=None) -> tuple:
    """Сопоставляет профессии, названные моделью, со списком должностей штатки.
    Возвращает (matched_positions, prof_conf): точное совпадение -> 1.0; иначе ближайшая
    должность по косинусу, если >= threshold; ниже — отбрасывается. embed можно подменить
    в тестах (name -> vector)."""
    embed = embed or _vec
    positions = [p for p in (positions or []) if (p or "").strip()]
    if not positions:
        return [], None
    pos_set = {p.casefold(): p for p in positions}

    matched, confs = [], []
    for name in named or []:
        name = (name or "").strip()
        if not name:
            continue
        if name.casefold() in pos_set:                 # точное совпадение
            p = pos_set[name.casefold()]
            if p not in matched:
                matched.append(p); confs.append(1.0)
            continue
        try:
            nv = embed(name)
            best_p, best_c = None, -1.0
            for p in positions:
                c = _cos(nv, embed(p))
                if c > best_c:
                    best_p, best_c = p, c
        except Exception:
            continue
        if best_p is not None and best_c >= threshold and best_p not in matched:
            matched.append(best_p); confs.append(round(best_c, 3))

    prof_conf = round(min(confs), 3) if confs else None
    return matched, prof_conf
