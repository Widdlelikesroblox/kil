"""ctypes bridge to the C engine (native/kingmaker.c).

If the shared library is missing, `available()` is False and the pure Python engine is used instead.
"""
from __future__ import annotations

import ctypes
import os
import sys
import threading
from typing import Callable, Optional, Sequence

_LIB_NAMES = {"win32": "kingmaker.dll", "darwin": "libkingmaker.dylib"}
_lib = None
_tried = False
ALTS = 24


class OutMove(ctypes.Structure):
    _fields_ = [("path", ctypes.c_int8 * 16), ("plen", ctypes.c_int8),
                ("ncaps", ctypes.c_int8), ("caps", ctypes.c_int8 * 12)]


class Alt(ctypes.Structure):
    _fields_ = [("mv", OutMove), ("score", ctypes.c_int32), ("lv", ctypes.c_int32 * 3)]


NO_SCORE = -32768


class Result(ctypes.Structure):
    _fields_ = [("score", ctypes.c_int32), ("depth", ctypes.c_int32), ("nmoves", ctypes.c_int32),
                ("npv", ctypes.c_int32), ("nalts", ctypes.c_int32), ("pad", ctypes.c_int32),
                ("nodes", ctypes.c_uint64), ("best", OutMove), ("pv", OutMove * 8), ("alts", Alt * ALTS)]


class COptions(ctypes.Structure):
    _fields_ = [("style", ctypes.c_int32), ("draw_value", ctypes.c_int32), ("threads", ctypes.c_int32),
                ("halfmove", ctypes.c_int32), ("nhist", ctypes.c_int32), ("want_alts", ctypes.c_int32),
                ("rules", ctypes.c_int32), ("pad", ctypes.c_int32)]


PROGRESS = ctypes.CFUNCTYPE(None, ctypes.c_int32, ctypes.c_int32, ctypes.c_uint64, ctypes.POINTER(OutMove))


def _candidates() -> list:
    name = _LIB_NAMES.get(sys.platform, "libkingmaker.so")
    here = os.path.dirname(os.path.abspath(__file__))
    paths = [os.path.join(here, name), os.path.join(os.path.dirname(here), name)]
    override = os.environ.get("KINGMAKER_LIB")
    if override:
        return [override]
    frozen = getattr(sys, "_MEIPASS", None)
    if frozen:
        paths = [os.path.join(frozen, "kingmaker", name), os.path.join(frozen, name)] + paths
    return paths


def _load():
    global _lib, _tried
    if _tried:
        return _lib
    _tried = True
    if os.environ.get("KINGMAKER_PURE_PYTHON"):
        return None
    for path in _candidates():
        if not os.path.exists(path):
            continue
        try:
            lib = ctypes.CDLL(path)
            lib.km_abi.restype = ctypes.c_int
            expect = ctypes.sizeof(Result) * 1000 + ctypes.sizeof(OutMove) + ctypes.sizeof(COptions)
            if lib.km_abi() != expect:
                continue   # an older library from a previous version
            lib.km_init.restype = None
            lib.km_init()
            lib.km_analyze.restype = ctypes.c_int
            lib.km_analyze.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                       ctypes.POINTER(ctypes.c_int32), PROGRESS, ctypes.POINTER(COptions),
                                       ctypes.c_void_p, ctypes.POINTER(Result)]
            lib.km_perft.restype = ctypes.c_uint64
            lib.km_perft.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
            lib.km_hardware_threads.restype = ctypes.c_int
            _lib = lib
            break
        except OSError:
            continue
    return _lib


def available() -> bool:
    return _load() is not None


def hardware_threads() -> int:
    lib = _load()
    if lib is not None:
        return max(1, int(lib.km_hardware_threads()))
    return os.cpu_count() or 1


def _move(o: OutMove, sq64, Move):
    return Move(tuple(sq64(o.path[i]) for i in range(o.plen)), tuple(sq64(o.caps[i]) for i in range(o.ncaps)))


def perft(board: Sequence[int], side: int, depth: int) -> int:
    lib = _load()
    arr = (ctypes.c_int8 * 64)(*board)
    return int(lib.km_perft(arr, side, depth))


def analyze(board, side, ms, on_progress, stop, max_depth, options, history, sq64, Move, Analysis, AltInfo):
    lib = _load()
    arr = (ctypes.c_int8 * 64)(*board)
    stop_flag = ctypes.c_int32(0)
    res = Result()
    o = COptions(options.style, options.draw_value, options.threads, options.halfmove, 0,
                 1 if options.want_alts else 0, options.rules, 0)
    hist_arr = None
    if history:
        flat = [v for b in history for v in b]
        hist_arr = (ctypes.c_int8 * len(flat))(*flat)
        o.nhist = len(history)

    def progress(depth, score, nodes, best):
        if on_progress:
            on_progress({"depth": depth, "score": score, "nodes": nodes, "best": _move(best.contents, sq64, Move)})

    cb = PROGRESS(progress)
    done = threading.Event()

    def watch():
        while not done.wait(0.05):
            if stop is not None and stop():
                stop_flag.value = 1
                return

    if stop is not None:
        threading.Thread(target=watch, daemon=True).start()
    try:
        rc = lib.km_analyze(arr, side, ms, max_depth, ctypes.byref(stop_flag), cb, ctypes.byref(o), hist_arr,
                            ctypes.byref(res))
    finally:
        done.set()
    if rc != 0:
        raise MemoryError("the C engine could not allocate its tables")
    if res.nmoves == 0:
        return Analysis(None, -10000, 0, 0, [], 0)
    pv = [_move(res.pv[i], sq64, Move) for i in range(res.npv)]
    alts = []
    for i in range(res.nalts):
        lv = tuple(None if res.alts[i].lv[k] == NO_SCORE else int(res.alts[i].lv[k]) for k in range(3))
        alts.append(AltInfo(_move(res.alts[i].mv, sq64, Move), lv))
    return Analysis(_move(res.best, sq64, Move), res.score, res.depth, int(res.nodes), pv, res.nmoves, alts)
