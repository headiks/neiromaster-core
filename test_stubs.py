"""
Заглушки тяжёлых зависимостей для тестов чистой логики.

Модули rag / classify / indexing / docpipe тянут qdrant_client, requests, psycopg
и настоящий config — на машине разработчика их может не быть, а логике, которую
мы проверяем (регэкспы ЧС, отбор папок, косинус, валидация JSON), они не нужны.
Раньше каждый тест копировал один и тот же блок подмен; теперь он один здесь.

Вызывать ДО импорта тестируемого модуля:

    import test_stubs
    cfg = test_stubs.install(embed_dim=8)
    import rag
"""

import sys
import types
from unittest.mock import MagicMock

QDRANT_MODEL_NAMES = ("VectorParams", "Distance", "PointStruct", "Filter", "FieldCondition",
                      "MatchValue", "MatchAny", "PayloadSchemaType")


def cosine(a, b) -> float:
    """Тот же контракт, что у config.cosine (0.0 для пустых/разной длины/нулевых)."""
    if not a or not b or len(a) != len(b):
        return 0.0
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return sum(x * y for x, y in zip(a, b)) / (na * nb)


def install(embed_dim: int = 8, embed=None, db: bool = True, psycopg: bool = False,
            stub_modules=("folders", "classify"), **config_extra) -> types.ModuleType:
    """
    Ставит заглушки и возвращает модуль-заглушку config (его можно донастроить).

    embed_dim / embed — что возвращает get_embedding (по умолчанию нулевой вектор).
    db           — подменить модуль db «пустышкой» (query/execute ничего не делают).
    psycopg      — добавить заглушки psycopg* (нужны docpipe).
    stub_modules — какие модули проекта подменить. Тест САМОГО classify должен
                   передать stub_modules=("folders",), иначе получит заглушку.
    """
    for name in ("qdrant_client", "qdrant_client.models", "requests") + tuple(stub_modules):
        sys.modules.setdefault(name, types.ModuleType(name))
    sys.modules["qdrant_client"].QdrantClient = lambda *a, **k: MagicMock()
    for name in QDRANT_MODEL_NAMES:
        setattr(sys.modules["qdrant_client.models"], name, MagicMock())

    if psycopg:
        for name in ("psycopg", "psycopg.types", "psycopg.rows", "psycopg_pool"):
            sys.modules.setdefault(name, types.ModuleType(name))
        sys.modules["psycopg.types.json"] = types.ModuleType("psycopg.types.json")
        sys.modules["psycopg.types.json"].Json = lambda x: x

    if db:
        mod = types.ModuleType("db")
        mod.query = mod.execute = lambda *a, **k: None
        sys.modules.setdefault("db", mod)

    cfg = sys.modules.get("config")
    if cfg is None or not isinstance(cfg, types.ModuleType) or hasattr(cfg, "__file__"):
        cfg = types.ModuleType("config")
        sys.modules["config"] = cfg
    cfg.OLLAMA_URL = "http://x"
    cfg.QDRANT_HOST = "x"
    cfg.QDRANT_PORT = 0
    cfg.EMBED_DIM = embed_dim
    cfg.get_embedding = embed or (lambda t: [0.0] * embed_dim)
    cfg.cosine = cosine
    for k, v in config_extra.items():
        setattr(cfg, k, v)
    return cfg


if __name__ == "__main__":
    assert cosine([1, 0], [1, 0]) == 1.0
    assert cosine([1, 0], [0, 1]) == 0.0
    assert cosine([], [1]) == 0.0 and cosine([0, 0], [1, 1]) == 0.0
    cfg = install(embed_dim=4)
    assert cfg.EMBED_DIM == 4 and cfg.get_embedding("x") == [0.0] * 4
    import qdrant_client                       # заглушка встала
    assert qdrant_client.QdrantClient() is not None
    print("test_stubs: заглушки и косинус — OK")
