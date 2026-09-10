import json
import time
from functools import lru_cache
from pathlib import Path

import numpy as np
from rapidfuzz.fuzz import ratio, token_set_ratio
from sklearn.feature_extraction.text import TfidfVectorizer

from .normalize import REQUIRED, compare, digest

FEATURE_VERSION = "pair-features-1.0.0"
FEATURE_NAMES = ["name_ratio", "name_tokens", "tfidf"] + [f"{k}_{s}" for k in REQUIRED for s in ("same", "conflict", "missing")] + ["coverage"]
INDEX_VERSION = "char-tfidf-2-4-v2"
DEFAULT_POLICY = {"engine": "rules", "high_threshold": 0.90, "margin": 0.08, "rule_version": "phone-cn-1.0.0", "feature_schema": FEATURE_VERSION, "category": "phone", "validated": False}


def features(a, b, tfidf=0):
    values = [ratio(a["search_text"], b["search_text"]) / 100, token_set_ratio(a["search_text"], b["search_text"]) / 100, tfidf]
    for k in REQUIRED:
        av, bv = a.get(k), b.get(k)
        known = av is not None and bv is not None
        values += [float(known and av == bv), float(known and av != bv), float(not known)]
    values.append(sum(a.get(k) is not None and b.get(k) is not None for k in REQUIRED) / len(REQUIRED))
    return dict(zip(FEATURE_NAMES, values))


def rule_score(f):
    return round(0.12 * f["name_ratio"] + 0.08 * f["tfidf"] + 0.23 * f["model_same"] + 0.12 * f["brand_same"] + sum(0.09 * f[f"{k}_same"] for k in ["ram", "storage", "color", "region", "pack_count"]), 6)


@lru_cache(maxsize=4)
def load_model(path, expected_hash):
    from lightgbm import Booster
    raw = Path(path).read_bytes()
    if digest(raw) != expected_hash:
        raise ValueError("MODEL_HASH_MISMATCH")
    return Booster(model_file=path)


class Matcher:
    def __init__(self, products, policy=None):
        self.products = products
        self.policy = policy or DEFAULT_POLICY
        self.vectorizer = TfidfVectorizer(analyzer="char", ngram_range=(2, 4), dtype=np.float32, max_features=150000)
        corpus = [p["normalized"]["search_text"] or "未知" for p in products]
        self.matrix = self.vectorizer.fit_transform(corpus) if corpus else None
        self.transposed = self.matrix.T.tocsr() if corpus else None
        self.by_model = {}
        for i, p in enumerate(products):
            m = p["normalized"].get("model")
            if m:
                self.by_model.setdefault(m, []).append(i)
        self.index_hash = digest({"version": INDEX_VERSION, "products": [(p["id"], p["normalized"]) for p in products]})

    def match(self, source):
        if not self.products:
            return "NO_CANDIDATE", []
        sparse = (self.vectorizer.transform([source["search_text"]]) @ self.transposed).tocsr()
        # A single catalog-length vector is bounded; no source x catalog matrix.
        similarities = np.zeros(len(self.products), dtype=np.float32)
        similarities[sparse.indices] = sparse.data
        exact = self.by_model.get(source.get("model"), [])
        retrieval_scores = similarities.copy()
        retrieval_scores[exact] += 0.25
        eligible = np.flatnonzero(similarities >= 0.08)
        eligible = np.union1d(eligible, exact).astype(np.int64)
        # Stable vectorized ordering avoids sorting tens of thousands of Python objects per query.
        indexes = eligible[np.lexsort((eligible, -retrieval_scores[eligible]))[:20]]
        rows = []
        for i in indexes:
            p = self.products[i]
            f = features(source, p["normalized"], float(similarities[i]))
            ev, conflicts, missing = compare(source, p["normalized"])
            score = rule_score(f)
            rows.append({"product_id": p["id"], "score": score, "features": f, "evidence": ev, "conflicts": conflicts, "missing": missing})
        if rows and self.policy["engine"] == "lightgbm":
            if self.policy.get("feature_schema") != FEATURE_VERSION:
                raise ValueError("FEATURE_SCHEMA_MISMATCH")
            model = load_model(self.policy["model_path"], self.policy["model_hash"])
            scores = model.predict(np.array([[r["features"][f] for f in FEATURE_NAMES] for r in rows]), num_threads=1)
            calibration = self.policy.get("calibration")
            if calibration:
                logits = np.log(np.clip(scores, 1e-6, 1-1e-6) / np.clip(1-scores, 1e-6, 1))
                scores = 1 / (1 + np.exp(-(logits * calibration["coef"] + calibration["intercept"])))
            for r, score in zip(rows, scores):
                r["score"] = float(score)
        rows.sort(key=lambda r: (bool(r["conflicts"]), -r["score"], r["product_id"]))
        if not rows:
            return "NO_CANDIDATE", []
        good = [r for r in rows if not r["conflicts"]]
        if not good:
            state = "CONFLICT"
        elif good[0]["missing"]:
            state = "REVIEW"
        elif good[0]["score"] >= self.policy["high_threshold"] and good[0]["score"] - (good[1]["score"] if len(good) > 1 else 0) >= self.policy["margin"]:
            state = "RECOMMENDED"
        else:
            state = "REVIEW"
        return state, [{**r, "rank": i + 1} for i, r in enumerate(rows)]
