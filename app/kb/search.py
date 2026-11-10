"""Поиск по базе знаний: BM25 со стеммингом русского языка. Роль A."""

import math
import re
from collections import Counter, defaultdict
from dataclasses import replace
from pathlib import Path

import snowballstemmer

from app.kb.ingest import load_corpus
from app.models import Chunk

_WORD = re.compile(r"[a-zа-я0-9]+")
_STEMMER = snowballstemmer.stemmer("russian")
_STEM_CACHE: dict[str, str] = {}
STOP_WORDS = frozenset(
    """
    и в во не что он на я с со как а то все она так его но да ты к у же вы за бы по только ее
    мне было вот от меня еще нет о из ему теперь когда даже ну ли если уже или ни быть был него
    до вас нибудь вам ведь там потом себя ничего ей может они тут где есть надо ней для мы тебя
    их чем была сам чтоб без будто чего раз тоже себе под будет ж тогда кто этот того потому
    этого какой совсем ним здесь этом один почти мой тем чтобы нее были куда зачем всех можно
    при об это эта эти мои моя мое про нужно подскажите пожалуйста расскажите хочу такое
    """.split()
)
MAX_PER_MATERIAL = 2


def tokenize(text: str) -> list[str]:
    """Слова в нижнем регистре, без стоп-слов, приведённые к основе."""
    tokens = []
    for word in _WORD.findall(text.lower().replace("ё", "е")):
        if word in STOP_WORDS:
            continue
        stem = _STEM_CACHE.get(word)
        if stem is None:
            stem = _STEM_CACHE[word] = _STEMMER.stemWord(word)
        tokens.append(stem)
    return tokens


class SearchIndex:
    def __init__(self, chunks: list[Chunk], k1: float = 1.5, b: float = 0.75) -> None:
        self._chunks = chunks
        self._k1 = k1
        self._b = b
        self._postings: dict[str, list[tuple[int, int]]] = defaultdict(list)
        self._lengths: list[int] = []
        for number, chunk in enumerate(chunks):
            tokens = tokenize(f"{chunk.material.title}\n{chunk.text}")
            self._lengths.append(len(tokens))
            for term, count in Counter(tokens).items():
                self._postings[term].append((number, count))
        total = len(chunks)
        self._average_length = (sum(self._lengths) / total) if total else 0.0
        self._idf = {
            term: math.log(1 + (total - len(postings) + 0.5) / (len(postings) + 0.5))
            for term, postings in self._postings.items()
        }
        self._unknown_idf = math.log(1 + (total + 0.5) / 0.5)

    def __len__(self) -> int:
        return len(self._chunks)

    def search(self, query: str, k: int = 6, min_coverage: float = 0.6) -> list[Chunk]:
        """До k фрагментов, лучший первым.

        Фрагмент подходит, только если покрывает не меньше min_coverage веса слов запроса
        (редкие слова весят больше). Так вопрос не по теме базы не получает случайных ответов.
        """
        terms = set(tokenize(query))
        if not terms or not self._chunks:
            return []
        query_weight = sum(self._idf.get(term, self._unknown_idf) for term in terms)
        scores: dict[int, float] = defaultdict(float)
        covered: dict[int, float] = defaultdict(float)
        for term in terms:
            idf = self._idf.get(term)
            if idf is None:
                continue
            for number, count in self._postings[term]:
                length_ratio = self._lengths[number] / self._average_length
                norm = self._k1 * (1 - self._b + self._b * length_ratio)
                scores[number] += idf * count * (self._k1 + 1) / (count + norm)
                covered[number] += idf
        ranked = sorted(
            (number for number in scores if covered[number] / query_weight >= min_coverage),
            key=lambda number: scores[number],
            reverse=True,
        )
        result: list[Chunk] = []
        per_material: Counter[str] = Counter()
        for number in ranked:
            chunk = self._chunks[number]
            if per_material[chunk.material.id] >= MAX_PER_MATERIAL:
                continue
            per_material[chunk.material.id] += 1
            result.append(replace(chunk, score=scores[number]))
            if len(result) == k:
                break
        return result


_index: SearchIndex | None = None


def set_index(index: SearchIndex | None) -> None:
    global _index
    _index = index


def load(data_dir: Path) -> SearchIndex:
    """Прочитать выгрузку и построить индекс. Вызывается один раз при старте."""
    index = SearchIndex(load_corpus(data_dir).chunks)
    set_index(index)
    return index


def search(query: str, k: int = 6) -> list[Chunk]:
    """Вернуть до k самых подходящих фрагментов, лучший первым."""
    if _index is None:
        raise RuntimeError("Индекс базы знаний не загружен: вызовите app.kb.search.load().")
    return _index.search(query, k)
