#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
compare_embed_quant.py -- did quantizing this embedding model change its answers?

WHY
---
"Q8 is fine for embeddings" is folklore until someone measures it on the actual
model. Quantization error matters more for retrieval than for chat: a chat model
samples from a distribution and small perturbations wash out, while retrieval
uses the vector's exact geometry -- the cosine between two points IS the answer.

WHAT IT MEASURES
----------------
  1. Vector drift        cosine(reference vector, quantized vector)
  2. Ranking stability    does each query still retrieve the same documents in
                          the same order?
  3. Cross-lingual        does a query in one language still retrieve the
     correctness          matching document written in ANOTHER language?

(2) matters more than (1): vectors can drift measurably while every ranking
holds, and rankings are what the application consumes.

(3) matters most for a MULTILINGUAL model, and it is not a quantization test
alone -- it also tells you whether the model does the job at all. This is where
quantization damage appears FIRST. A multilingual model's capacity is spread
unevenly across languages; the ones with the least weight mass have the least
margin to lose, and in a Mixture-of-Experts model a token's output depends on
which experts fire, so rounding can in principle nudge a borderline routing
decision. English staying perfect tells you very little about Swahili.

USAGE
-----
    python compare_embed_quant.py <reference.gguf> <quantized.gguf>

Run it from the backend\\ directory so llama-server.exe and its DLLs resolve.
Each model is served in turn on a scratch port, then shut down. Nothing is
written to disk; no application state is touched.

READING THE RESULT
------------------
    min cosine >= 0.9995, 0 ranking changes, cross-lingual equal -> free
    cross-lingual accuracy DROPS in the quantized model           -> too far
    cross-lingual accuracy is low in BOTH                         -> not a
        quantization problem; the model or the prefixes are wrong
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

# This file is deliberately NOT ASCII -- the test data is the point. Windows
# consoles default to a legacy codepage and would raise UnicodeEncodeError on
# the first Japanese character, killing the run after the slow part is done.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

PORT = int(os.environ.get("COMPARE_PORT", "11439"))   # scratch, not a real tier
HOST = "127.0.0.1"
CTX = int(os.environ.get("COMPARE_CTX", "512"))

# --------------------------------------------------------------------------
# Documents grouped by MEANING, not by language.
#
# Grouping by topic rather than pairing exact translations is deliberate: it
# measures the capability that matters ("did it find something that means the
# right thing") without depending on any one translation being the single
# canonical match. Nuance between two good translations should not count as a
# failure.
# --------------------------------------------------------------------------
TOPICS = {
    "library_hours": [
        ("en", "The library closes at eight in the evening."),
        ("es", "La biblioteca cierra a las ocho de la tarde."),
        ("fr", "La bibliotheque ferme a huit heures du soir."),
        ("de", "Die Bibliothek schliesst um acht Uhr abends."),
        ("pt", "A biblioteca fecha as oito da noite."),
        ("ru", "Библиотека закрывается в восемь вечера."),
        ("zh", "图书馆晚上八点关门。"),
        ("ja", "図書館は夜八時に閉まります。"),
        ("ko", "도서관은 저녁 여덟 시에 문을 닫습니다."),
        ("ar", "تغلق المكتبة في الثامنة مساء."),
        ("hi", "पुस्तकालय रात आठ बजे बंद हो जाता है।"),
        ("tr", "Kutuphane aksam sekizde kapaniyor."),
        ("vi", "Thu vien dong cua luc tam gio toi."),
        ("sw", "Maktaba hufungwa saa mbili usiku."),
    ],
    "airport_train": [
        ("en", "The train to the airport leaves every fifteen minutes."),
        ("es", "El tren al aeropuerto sale cada quince minutos."),
        ("de", "Der Zug zum Flughafen faehrt alle fuenfzehn Minuten."),
        ("ru", "Поезд в аэропорт отправляется каждые пятнадцать минут."),
        ("zh", "去机场的火车每十五分钟一班。"),
        ("ja", "空港行きの電車は十五分ごとに出発します。"),
        ("ar", "يغادر القطار الى المطار كل خمس عشرة دقيقة."),
        ("hi", "हवाई अड्डे की ट्रेन हर पंद्रह मिनट में जाती है।"),
    ],
    "db_config": [
        ("en", "Postgres reads its configuration from postgresql.conf at startup."),
        ("en", "MySQL reads its configuration from my.cnf at startup."),
        ("de", "Postgres liest beim Start seine Konfiguration aus postgresql.conf."),
        ("zh", "Postgres 启动时从 postgresql.conf 读取配置。"),
    ],
    "distractor": [
        ("en", "The mitochondrion is the powerhouse of the cell."),
        ("en", "Rainfall in the Atacama desert is measured in millimetres per decade."),
        ("fr", "Les Jeux olympiques d'hiver de 1998 ont eu lieu a Nagano, au Japon."),
        ("ja", "アタカマ砂漠の降水量は十年ごとにミリメートルで測られます。"),
        ("ru", "Митохондрия - это энергетическая станция клетки."),
    ],
}

# Near-duplicates: one word apart. Fine distinctions lose margin first under
# quantization, so these are the sensitive part of the drift measurement.
TOPICS["fine_distinction"] = [
    ("en", "The cat sat on the mat in the warm afternoon sun."),
    ("en", "The cat sat on the mat in the cold afternoon rain."),
    ("es", "El gato se sento en la alfombra bajo el sol de la tarde."),
]

DOCUMENTS: list[str] = []
DOC_TOPIC: list[str] = []
DOC_LANG: list[str] = []
for topic, rows in TOPICS.items():
    for lang, text in rows:
        DOCUMENTS.append(text)
        DOC_TOPIC.append(topic)
        DOC_LANG.append(lang)

# Each query is asked in a language and must retrieve a document of the right
# MEANING -- usually written in a different language. That is the whole promise
# of a multilingual embedder.
QUERIES = [
    ("en", "when does the library close",              "library_hours"),
    ("zh", "图书馆几点关门",                              "library_hours"),
    ("ar", "متى تغلق المكتبة",                           "library_hours"),
    ("sw", "maktaba hufungwa saa ngapi",                "library_hours"),
    ("hi", "पुस्तकालय कब बंद होता है",                      "library_hours"),
    ("ru", "во сколько закрывается библиотека",         "library_hours"),
    ("ja", "空港までの電車はどのくらいの間隔ですか",          "airport_train"),
    ("es", "cada cuanto sale el tren al aeropuerto",    "airport_train"),
    ("tr", "havaalanina tren ne siklikta kalkiyor",     "airport_train"),
    ("en", "how does a database find its config file",  "db_config"),
    ("zh", "数据库启动时在哪里读取配置",                   "db_config"),
    ("en", "where did the cat sit",                     "fine_distinction"),
]


def _post(url: str, payload: dict, timeout: float = 180.0):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def wait_ready(timeout: float = 240.0) -> bool:
    """Poll until the server answers. A fixed sleep either wastes time or fails
    on a cold file cache."""
    end = time.time() + timeout
    while time.time() < end:
        try:
            _post(f"http://{HOST}:{PORT}/v1/embeddings", {"input": ["ping"]}, timeout=5)
            return True
        except Exception:
            time.sleep(1.0)
    return False


def serve(model: Path):
    exe = Path("llama-server.exe")
    if not exe.exists():
        exe = Path(__file__).resolve().parent.parent / "backend" / "llama-server.exe"
    argv = [str(exe), "-m", str(model), "--host", HOST, "--port", str(PORT),
            "--ctx-size", str(CTX), "-ngl", "0", "--embedding", "--pooling", "mean"]
    return subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def embed_all(texts, task):
    """Prefix exactly as the application does, or this is measuring something
    the app never asks for."""
    payload = [f"{task}: {t}" for t in texts]
    out = []
    for i in range(0, len(payload), 16):          # modest batches; ctx is small
        d = _post(f"http://{HOST}:{PORT}/v1/embeddings", {"input": payload[i:i + 16]})
        out.extend(row["embedding"] for row in d["data"])
    return out


def collect(model: Path):
    proc = serve(model)
    try:
        if not wait_ready():
            raise SystemExit(f"  {model.name}: server never became ready. Does "
                             f"this llama.cpp build support the architecture?")
        return (embed_all(DOCUMENTS, "search_document"),
                embed_all([q[1] for q in QUERIES], "search_query"))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except Exception:
            proc.kill()


def cos(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def ranking(qvec, docvecs):
    return sorted(range(len(docvecs)), key=lambda i: -cos(qvec, docvecs[i]))


def cross_lingual_score(qvecs, docvecs):
    """Two numbers, because they answer different questions.

    `correct` lets the query match a document in its OWN language. A Chinese
    query matching the Chinese sentence is right, but it does not demonstrate
    anything multilingual -- monolingual models do that too.

    `cross` HIDES every document in the query's own language and asks again.
    That is the multilingual promise stated as a test: with the easy answer
    removed, does the model still find the meaning in another script? It is
    also where quantization damage lands first, because the languages with the
    least weight mass have the least margin to lose.
    """
    correct, cross, detail = 0, 0, []
    for i, (qlang, qtext, want) in enumerate(QUERIES):
        order = ranking(qvecs[i], docvecs)
        top = order[0]
        ok = DOC_TOPIC[top] == want

        foreign = [j for j in order if DOC_LANG[j] != qlang]
        xtop = foreign[0] if foreign else None
        xok = xtop is not None and DOC_TOPIC[xtop] == want

        correct += ok
        cross += xok
        detail.append((qlang, qtext, want, DOC_TOPIC[top], DOC_LANG[top], ok,
                       DOC_TOPIC[xtop] if xtop is not None else "-",
                       DOC_LANG[xtop] if xtop is not None else "-", xok))
    return correct, cross, detail


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 1
    ref, quant = Path(sys.argv[1]), Path(sys.argv[2])
    for p in (ref, quant):
        if not p.exists():
            print(f"  not found: {p}")
            return 1

    print(f"\n  reference : {ref.name}  ({ref.stat().st_size/1048576:.0f} MB)")
    print(f"  quantized : {quant.name}  ({quant.stat().st_size/1048576:.0f} MB)")
    print(f"  size      : {100*(1-quant.stat().st_size/ref.stat().st_size):.0f}% smaller")
    langs = sorted(set(DOC_LANG))
    print(f"  corpus    : {len(DOCUMENTS)} docs / {len(QUERIES)} queries / "
          f"{len(langs)} languages ({' '.join(langs)})\n")

    print("  serving reference...")
    ref_docs, ref_qs = collect(ref)
    print("  serving quantized...")
    q_docs, q_qs = collect(quant)

    print("\n  --- 1. did individual vectors move? ---")
    sims = [cos(a, b) for a, b in zip(ref_docs, q_docs)] + \
           [cos(a, b) for a, b in zip(ref_qs, q_qs)]
    worst_i = min(range(len(sims)), key=lambda i: sims[i])
    worst = sims[worst_i]
    where = (f"doc[{worst_i}] {DOC_LANG[worst_i]}" if worst_i < len(ref_docs)
             else f"query[{worst_i-len(ref_docs)}] "
                  f"{QUERIES[worst_i-len(ref_docs)][0]}")
    print(f"     vectors compared : {len(sims)}")
    print(f"     mean cosine      : {sum(sims)/len(sims):.6f}")
    print(f"     min  cosine      : {worst:.6f}   ({where})")

    print("\n  --- 2. did the ranking change where it matters? ---")
    # Top-k, NOT the full permutation.
    #
    # Comparing the entire ordering of 34 documents measures noise, not damage.
    # Fourteen of them are translations of one sentence and are near-ties by
    # construction; a 0.0001 nudge swaps items ranked 20th and 21st, which no
    # application will ever look at. An earlier version of this script reported
    # "12/12 rankings changed" for a model whose top-1 was perfect in every
    # language -- a scary number describing nothing.
    #
    # Retrieval consumes the head of the list. So: does the head agree, and
    # when it first disagrees, how far down and by what margin?
    def topk_report(k):
        exact = same_set = 0
        for i in range(len(QUERIES)):
            r = ranking(ref_qs[i], ref_docs)[:k]
            q = ranking(q_qs[i], q_docs)[:k]
            exact += (r == q)
            same_set += (set(r) == set(q))
        return exact, same_set

    for k in (1, 3, 5):
        exact, same_set = topk_report(k)
        n = len(QUERIES)
        print(f"     top-{k}: {exact}/{n} identical order, "
              f"{same_set}/{n} same documents")

    # Where does it first diverge, and is that a real gap or a coin-flip?
    depths, margins = [], []
    for i in range(len(QUERIES)):
        r = ranking(ref_qs[i], ref_docs)
        q = ranking(q_qs[i], q_docs)
        d = next((j for j in range(len(r)) if r[j] != q[j]), None)
        if d is None:
            continue
        depths.append(d + 1)
        a = cos(ref_qs[i], ref_docs[r[d]])
        b = cos(ref_qs[i], ref_docs[r[d + 1]]) if d + 1 < len(r) else a
        margins.append(abs(a - b))
    if depths:
        print(f"     first divergence: rank {min(depths)} at the earliest, "
              f"rank {sum(depths)/len(depths):.1f} on average")
        print(f"     cosine gap at the swap: {min(margins):.6f} min, "
              f"{sum(margins)/len(margins):.6f} mean")
        print(f"     (a gap near zero means those documents were tied -- "
              f"the order between them was never meaningful)")
    else:
        print("     no divergence anywhere in the ordering")
    top1_exact = topk_report(1)[0]
    changed = len(QUERIES) - top1_exact

    print("\n  --- 3. cross-lingual retrieval (the multilingual promise) ---")
    r_ok, r_cross, r_detail = cross_lingual_score(ref_qs, ref_docs)
    q_ok, q_cross, q_detail = cross_lingual_score(q_qs, q_docs)
    n = len(QUERIES)
    print(f"     reference : {r_ok}/{n} top-1 correct   |  "
          f"{r_cross}/{n} correct with same-language docs HIDDEN")
    print(f"     quantized : {q_ok}/{n} top-1 correct   |  "
          f"{q_cross}/{n} correct with same-language docs HIDDEN")
    for i in range(n):
        rl, rt, want, got_r, _, ok_r = r_detail[i][:6]
        ok_q = q_detail[i][5]
        got_q = q_detail[i][3]
        if ok_r != ok_q:
            print(f"     [{rl}] {rt}")
            print(f"        reference -> {got_r} ({'ok' if ok_r else 'WRONG'})")
            print(f"        quantized -> {got_q} ({'ok' if ok_q else 'WRONG'})")
    xmiss = [d for d in q_detail if not d[8]]
    if xmiss:
        print("     cross-language misses (quantized):")
        for d in xmiss:
            print(f"        [{d[0]}] {d[1]}")
            print(f"           wanted {d[2]}, best foreign-language hit was "
                  f"{d[6]} [{d[7]}]")

    print("\n  --- verdict ---")
    if q_cross < r_cross:
        print(f"     Cross-language retrieval DEGRADED ({r_cross} -> {q_cross} "
              f"with same-language answers hidden). This is the failure that "
              f"matters for a multilingual model, and English would not have "
              f"shown it. Try Q6_K or BF16.")
    elif q_ok < r_ok:
        print(f"     Cross-lingual accuracy DROPPED ({r_ok} -> {q_ok}). The "
              f"quantization went too far for multilingual use, even if the "
              f"English cases look fine. Try a higher precision.")
    elif changed:
        print(f"     {changed} ranking(s) changed. Usable, but verify against "
              f"your real corpus before shipping.")
    elif r_ok < n:
        print(f"     Quantization held ({r_ok}/{n} in BOTH models), but the "
              f"model itself missed {n-r_ok}. That is a model/prefix question, "
              f"not a quantization one -- the quantized copy is no worse.")
    elif worst >= 0.9995:
        print("     Quantization is effectively free, including across "
              "languages. Ship it.")
    else:
        print(f"     Every ranking and every cross-lingual match held; vectors "
              f"moved slightly (min {worst:.6f}). Fine for retrieval.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
