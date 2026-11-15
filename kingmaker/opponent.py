"""Opponent model: predicts which move a human opponent will play, and learns from what they actually play.

The engine gives a score for every legal move at three search depths (see AltInfo). A human sees only a few
moves ahead and sometimes slips, so the best move at full depth is a poor guess for what they will play.
Instead we keep a family of models, each saying "the opponent picks moves with probability proportional to
exp(score / T), judging the position at depth level L". After every observed move the models that gave the
real move a high probability gain weight, so the prediction adapts to the person you are playing.

Pure Python, no dependencies.
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence, Tuple

LEVELS = (0, 1, 2)                       # depth 4, depth 8, deep
TEMPS = (12, 25, 50, 100, 200, 600)      # centipawns; small = plays the best move at that level, large = near random
FORGET = 0.97                            # old evidence fades so the model can follow a player who changes
MIN_P = 1e-3


def _level_scores(alts: Sequence, level: int) -> Optional[List[float]]:
    out = []
    for a in alts:
        v = a.levels[level]
        if v is None:
            return None
        out.append(float(v))
    return out


def _softmax(scores: Sequence[float], temp: float) -> List[float]:
    top = max(scores)
    ex = [math.exp((s - top) / temp) for s in scores]
    z = sum(ex)
    return [e / z for e in ex]


class OpponentModel:
    def __init__(self, ll: Optional[Dict[str, float]] = None) -> None:
        self.ll: Dict[Tuple[int, int], float] = {(l, t): 0.0 for l in LEVELS for t in TEMPS}
        if ll:
            for key, v in ll.items():
                try:
                    l, t = key.split(",")
                    if (int(l), int(t)) in self.ll:
                        self.ll[(int(l), int(t))] = float(v)
                except (ValueError, AttributeError):
                    pass
        self.moves_seen = 0
        self.top1_hits = 0          # the model's favourite was the move played
        self.best_hits = 0          # the engine's strongest move was the move played

    # -- persistence
    def to_dict(self) -> Dict[str, float]:
        return {f"{l},{t}": round(v, 4) for (l, t), v in self.ll.items()}

    def reset(self) -> None:
        for k in self.ll:
            self.ll[k] = 0.0
        self.moves_seen = self.top1_hits = self.best_hits = 0

    # -- prediction
    def _weights(self, available: Sequence[int]) -> Dict[Tuple[int, int], float]:
        keys = [(l, t) for (l, t) in self.ll if l in available]
        top = max(self.ll[k] for k in keys)
        w = {k: math.exp(self.ll[k] - top) for k in keys}
        z = sum(w.values())
        return {k: v / z for k, v in w.items()}

    def predict(self, alts: Sequence) -> List[Tuple[object, float]]:
        """Return [(move, probability)] sorted by probability, highest first."""
        if not alts:
            return []
        avail = [l for l in LEVELS if _level_scores(alts, l) is not None]
        if not avail:
            return []
        weights = self._weights(avail)
        probs = [0.0] * len(alts)
        for (l, t), w in weights.items():
            p = _softmax(_level_scores(alts, l), t)
            for i, pi in enumerate(p):
                probs[i] += w * pi
        ranked = sorted(zip((a.move for a in alts), probs), key=lambda x: -x[1])
        return ranked

    def engine_best(self, alts: Sequence):
        avail = [l for l in LEVELS if _level_scores(alts, l) is not None]
        if not avail:
            return None
        sc = _level_scores(alts, avail[-1])
        return alts[max(range(len(sc)), key=lambda i: sc[i])].move

    # -- learning
    def observe(self, alts: Sequence, played_path: Tuple[int, ...]) -> Optional[bool]:
        """Learn from the move the opponent really played. Returns True if our favourite was right,
        False if it was wrong, None if the move could not be scored."""
        idx = next((i for i, a in enumerate(alts) if a.move.path == tuple(played_path)), None)
        if idx is None:
            return None
        ranked = self.predict(alts)
        hit = bool(ranked) and ranked[0][0].path == tuple(played_path)
        best = self.engine_best(alts)
        for k in self.ll:
            self.ll[k] *= FORGET
        for l in LEVELS:
            sc = _level_scores(alts, l)
            if sc is None:
                continue
            for t in TEMPS:
                p = max(_softmax(sc, t)[idx], MIN_P)
                self.ll[(l, t)] += math.log(p)
        self.moves_seen += 1
        self.top1_hits += 1 if hit else 0
        self.best_hits += 1 if (best is not None and best.path == tuple(played_path)) else 0
        return hit

    def describe(self) -> str:
        """A short plain-language read on the opponent, from the moves seen so far."""
        if self.moves_seen < 3:
            return "Still learning how your opponent plays."
        w = self._weights(list(LEVELS))
        noise = sum(v * t for (_l, t), v in w.items())
        slips = "rarely slips" if noise < 40 else "sometimes slips" if noise < 120 else "plays unpredictably"
        return f"Your opponent {slips}. My favourite was right {self.top1_hits} of {self.moves_seen} times."
