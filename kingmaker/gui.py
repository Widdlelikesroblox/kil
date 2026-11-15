"""Kingmaker desktop app (Tkinter). Run with `python -m kingmaker`."""
from __future__ import annotations

import json
import math
import os
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk
from typing import List, Optional

from . import engine as E
from .opponent import OpponentModel

SQ = 80                      # square size in pixels
LIGHT, DARK = "#E3E7EC", "#34506B"
BLACK_DISC, BLACK_RIM, BLACK_RING = "#1C1F25", "#8895A7", "#3A424E"
RED_DISC, RED_RIM, RED_RING = "#D8483F", "#F6A9A2", "#A52E27"
GOLD, GOLD_INK = "#F2B632", "#1B1405"
BG, PANEL, INK, MUTED, LINE = "#EDF0F4", "#FFFFFF", "#131A22", "#566273", "#D5DBE4"
SETTINGS_PATH = os.path.join(os.path.expanduser("~"), ".kingmaker.json")
STYLE_INDEX = {"balanced": E.STYLE_BALANCED, "safe": E.STYLE_SAFE, "aggressive": E.STYLE_AGGRESSIVE}
STYLE_HELP = {
    "balanced": "Plays the strongest move it can find.",
    "safe": "Takes no chances. Welcomes draws, avoids risky structure, prefers moves that give you the fewest forcing replies.",
    "aggressive": "Avoids draws and presses for a win. Use it when you are ahead or need to win.",
}
RULE_HELP = {
    E.RULES_AMERICAN: "Standard American checkers. If you can jump, you must.",
    E.RULES_OPTIONAL: "Jumps are optional. You and your opponent may skip a capture. Once you start a jump you finish the chain.",
}
THINK_CHOICES = {"Quick (1.5 s)": 1500, "Strong (5 s)": 5000, "Deep (12 s)": 12000, "Maximum (30 s)": 30000,
                "Until I move": 600000}
CROWN = [(-16, 11), (-18, -9), (-8, 0), (0, -13), (8, 0), (18, -9), (16, 11)]
PANEL_REFRESH = 0.25         # seconds between side-panel redraws while a search is running


def name(side: int) -> str:
    return "Black" if side > 0 else "Red"


def square_number(sq: int) -> int:
    return E.sq32(sq) + 1


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Kingmaker")
        self.configure(bg=BG)
        self.minsize(1040, 720)
        try:
            ttk.Style().theme_use("clam")
        except tk.TclError:
            pass
        from . import native
        # game state
        self.me = 1
        self.rules = E.RULES_AMERICAN
        self.first = 1
        self.board: List[int] = E.start_board()
        self.turn = 1
        self.mode = "play"
        self.brush = "meMan"
        self.sel: List[int] = []
        self.hist: list = []
        self.last: Optional[tuple] = None
        self.legal: List[E.Move] = []
        self.flip: Optional[bool] = None
        self.msg = ""
        # settings
        self.show_nums = tk.BooleanVar(value=True)
        self.think_ms = 12000
        self.style = "balanced"
        self.auto_safe = True
        self.auto_margin = 0.5
        self.auto_on = False
        self.show_alts = True
        self.show_settings = False
        self.threads = max(1, min(8, native.hardware_threads() - 1)) if native.available() else 1
        self.model = OpponentModel()
        self._load_settings()
        # analysis
        self.an: dict = {}
        self.req = 0
        self.stop_ev: Optional[threading.Event] = None
        self.q: "queue.Queue" = queue.Queue()
        self.predict: list = []
        self.pending = None            # (board tuple, alts) from the opponent's turn, used to learn from their move
        self.pred_log: List[bool] = []
        self.new_armed = 0.0
        # drawing caches
        self._static_sig = None
        self._panel_dirty = False
        self._panel_at = 0.0
        self._build_chooser()
        self.bind("<Control-z>", lambda _e: self.undo())
        self.bind("<Return>", lambda _e: self.apply_best() if self.mode == "play" and not self.show_settings else None)
        self.after(100, self._poll)

    # ------------------------------------------------------------------ settings
    def _load_settings(self) -> None:
        try:
            with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                d = json.load(f)
            if d.get("style") in STYLE_INDEX:
                self.style = d["style"]
            self.auto_safe = bool(d.get("auto_safe", self.auto_safe))
            self.auto_margin = min(3.0, max(0.1, float(d.get("auto_margin", self.auto_margin))))
            self.threads = min(32, max(1, int(d.get("threads", self.threads))))
            self.show_alts = bool(d.get("show_alts", self.show_alts))
            self.think_ms = int(d.get("think_ms", self.think_ms))
            self.rules = E.RULES_OPTIONAL if d.get("rules") == E.RULES_OPTIONAL else E.RULES_AMERICAN
            self.first = -1 if d.get("first") == -1 else 1
            self.model = OpponentModel(d.get("opponent_model"))
        except (OSError, ValueError, TypeError, AttributeError):
            pass

    def _save_settings(self) -> None:
        try:
            with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
                json.dump({"style": self.style, "auto_safe": self.auto_safe, "auto_margin": self.auto_margin,
                           "threads": self.threads, "show_alts": self.show_alts, "think_ms": self.think_ms,
                           "rules": self.rules, "first": self.first, "opponent_model": self.model.to_dict()}, f)
        except OSError:
            pass

    def effective_style(self) -> str:
        """The style used for the next search. The opponent is always modelled as playing its best."""
        if self.turn != self.me:
            return "balanced"
        if self.style == "balanced" and self.auto_safe and self.auto_on:
            return "safe"
        return self.style

    def _update_auto(self, my_score: int) -> bool:
        margin = self.auto_margin * 100
        if not self.auto_on and my_score <= -margin:
            self.auto_on = True
            return True
        if self.auto_on and my_score >= -margin / 2:
            self.auto_on = False
            return True
        return False

    def reversible_history(self) -> tuple:
        """(plies since the last capture or man move, boards since then) for draw detection."""
        hm = 0
        for e in reversed(self.hist):
            if e.get("irrev"):
                break
            hm += 1
        boards = [e["board"] for e in self.hist[len(self.hist) - hm:]] if hm else []
        return hm, boards

    # ------------------------------------------------------------------ chooser
    def _build_chooser(self) -> None:
        self.chooser = tk.Frame(self, bg=BG)
        self.chooser.pack(fill="both", expand=True)
        box = tk.Frame(self.chooser, bg=BG)
        box.place(relx=0.5, rely=0.5, anchor="center")
        tk.Label(box, text="Kingmaker", font=("Helvetica", 34, "bold"), bg=BG, fg=INK).pack(anchor="w")
        tk.Label(box, text="Set up your game", font=("Helvetica", 16), bg=BG, fg=MUTED).pack(anchor="w", pady=(2, 14))

        self._c_rules = tk.IntVar(value=self.rules)
        self._c_first = tk.IntVar(value=self.first)
        opts = tk.Frame(box, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
        opts.pack(fill="x")
        inner = tk.Frame(opts, bg=PANEL)
        inner.pack(fill="x", padx=16, pady=12)
        tk.Label(inner, text="Game type", font=("Helvetica", 11, "bold"), bg=PANEL, fg=INK).pack(anchor="w")
        for val in (E.RULES_AMERICAN, E.RULES_OPTIONAL):
            tk.Radiobutton(inner, text=E.RULE_NAMES[val], value=val, variable=self._c_rules, bg=PANEL, fg=INK,
                           activebackground=PANEL, selectcolor=PANEL, anchor="w").pack(anchor="w")
            tk.Label(inner, text=RULE_HELP[val], bg=PANEL, fg=MUTED, font=("Helvetica", 9), wraplength=420,
                     justify="left").pack(anchor="w", padx=(24, 0))
        tk.Label(inner, text="Who moves first", font=("Helvetica", 11, "bold"), bg=PANEL, fg=INK).pack(anchor="w", pady=(10, 0))
        row = tk.Frame(inner, bg=PANEL)
        row.pack(anchor="w")
        for val, label in ((1, "Black (standard)"), (-1, "Red")):
            tk.Radiobutton(row, text=label, value=val, variable=self._c_first, bg=PANEL, fg=INK,
                           activebackground=PANEL, selectcolor=PANEL).pack(side="left", padx=(0, 14))

        tk.Label(box, text="Which side are you playing?", font=("Helvetica", 14), bg=BG, fg=INK).pack(anchor="w", pady=(16, 6))
        row = tk.Frame(box, bg=BG)
        row.pack(anchor="w")
        for side, sub in ((1, "Black"), (-1, "Red")):
            f = tk.Frame(row, bg=PANEL, highlightbackground=LINE, highlightthickness=1, cursor="hand2")
            f.pack(side="left", padx=(0, 14))
            c = tk.Canvas(f, width=64, height=64, bg=PANEL, highlightthickness=0)
            c.pack(side="left", padx=12, pady=10)
            self._piece(c, 32, 32, side, False, 26)
            t = tk.Frame(f, bg=PANEL)
            t.pack(side="left", padx=(0, 22))
            tk.Label(t, text=sub, font=("Helvetica", 20, "bold"), bg=PANEL, fg=INK).pack(anchor="w")
            tk.Label(t, text="Play as " + sub, font=("Helvetica", 10), bg=PANEL, fg=MUTED).pack(anchor="w")
            for w in (f, c, t, *t.winfo_children()):
                w.bind("<Button-1>", lambda _e, s=side: self._choose(s))
        tk.Label(box, text="8x8 board · kings move both ways · you can change everything later in Settings",
                 font=("Helvetica", 10), bg=BG, fg=MUTED).pack(anchor="w", pady=(18, 0))

    def _choose(self, side: int) -> None:
        self.me = side
        self.rules = self._c_rules.get()
        self.first = self._c_first.get()
        self.turn = self.first
        self.chooser.destroy()
        self._build_main()
        self._save_settings()
        self.refresh_legal()
        self.request()
        self.render()

    # ------------------------------------------------------------------ main window
    def _build_main(self) -> None:
        self.main = tk.Frame(self, bg=BG)
        self.main.pack(fill="both", expand=True, padx=16, pady=14)
        left = tk.Frame(self.main, bg=BG)
        left.pack(side="left", anchor="n")
        self.canvas = tk.Canvas(left, width=SQ * 8, height=SQ * 8, bg=DARK, highlightthickness=4, highlightbackground="#22364A")
        self.canvas.pack()
        self.canvas.bind("<Button-1>", self._on_click)
        tools = tk.Frame(left, bg=BG)
        tools.pack(fill="x", pady=(10, 0))
        ttk.Button(tools, text="Flip board", command=self._flip).pack(side="left")
        ttk.Checkbutton(tools, text="Square numbers", variable=self.show_nums, command=self.render_board).pack(side="left", padx=12)
        tk.Label(tools, text="Think time", bg=BG, fg=MUTED).pack(side="left")
        self.think_box = ttk.Combobox(tools, values=list(THINK_CHOICES), state="readonly", width=14)
        self.think_box.set(next((k for k, v in THINK_CHOICES.items() if v == self.think_ms), "Deep (12 s)"))
        self.think_box.pack(side="left", padx=6)
        self.think_box.bind("<<ComboboxSelected>>", self._on_think)
        self.engine_label = tk.Label(left, text="", bg=BG, fg=MUTED, font=("Helvetica", 9))
        self.engine_label.pack(anchor="w", pady=(6, 0))

        right = tk.Frame(self.main, bg=BG)
        right.pack(side="left", fill="both", expand=True, padx=(18, 0))
        top = tk.Frame(right, bg=BG)
        top.pack(fill="x")
        self.rules_label = tk.Label(top, text="", bg=GOLD, fg=GOLD_INK, font=("Helvetica", 10, "bold"), padx=8, pady=2)
        self.rules_label.pack(side="left")
        self.mode_play = ttk.Button(top, text="Play", command=lambda: self.set_mode("play"))
        self.mode_play.pack(side="left", padx=(10, 0))
        self.mode_setup = ttk.Button(top, text="Set up", command=lambda: self.set_mode("setup"))
        self.mode_setup.pack(side="left", padx=4)
        self.settings_btn = ttk.Button(top, text="Settings", command=self._toggle_settings)
        self.settings_btn.pack(side="left")
        self.swap_btn = ttk.Button(top, command=self._swap)
        self.swap_btn.pack(side="right")
        self.new_btn = ttk.Button(top, text="New game", command=self._new_game_click)
        self.new_btn.pack(side="right", padx=4)

        holder = tk.Frame(right, bg=BG)
        holder.pack(fill="both", expand=True, pady=(12, 0))
        self.pcanvas = tk.Canvas(holder, bg=BG, highlightthickness=0, width=390)
        sb = ttk.Scrollbar(holder, orient="vertical", command=self.pcanvas.yview)
        self.pcanvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.pcanvas.pack(side="left", fill="both", expand=True)
        self.panel = tk.Frame(self.pcanvas, bg=BG)
        self.pwin = self.pcanvas.create_window((0, 0), window=self.panel, anchor="nw")
        self.panel.bind("<Configure>", lambda _e: self.pcanvas.configure(scrollregion=self.pcanvas.bbox("all")))
        self.pcanvas.bind("<Configure>", lambda e: self.pcanvas.itemconfigure(self.pwin, width=e.width))
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.pcanvas.bind_all(seq, self._wheel)

    def _wheel(self, e: tk.Event) -> None:
        if getattr(e, "num", 0) == 4:
            step = -1
        elif getattr(e, "num", 0) == 5:
            step = 1
        else:
            step = -1 if getattr(e, "delta", 0) > 0 else 1
        self.pcanvas.yview_scroll(step, "units")

    # ------------------------------------------------------------------ drawing
    def _piece(self, c: tk.Canvas, x: float, y: float, side: int, king: bool, r: float, tag: Optional[str] = None) -> None:
        f, rim, ring = (BLACK_DISC, BLACK_RIM, BLACK_RING) if side > 0 else (RED_DISC, RED_RIM, RED_RING)
        c.create_oval(x - r, y - r + 4, x + r, y + r + 4, fill="#1A2733", outline="", tags=tag)
        c.create_oval(x - r, y - r, x + r, y + r, fill=f, outline=rim, width=2, tags=tag)
        k = r * 0.69
        c.create_oval(x - k, y - k, x + k, y + k, outline=ring, width=2, tags=tag)
        if king:
            s = r / 42 * 0.95
            c.create_polygon([(x + px * s, y + py * s) for px, py in CROWN], fill="#FFD166", outline="", tags=tag)
            c.create_rectangle(x - 16 * s, y + 12 * s, x + 16 * s, y + 16 * s, fill="#FFD166", outline="", tags=tag)

    def _xy(self, sq: int) -> tuple:
        r, c = sq >> 3, sq & 7
        if self._flipped():
            r, c = 7 - r, 7 - c
        return c * SQ + SQ / 2, r * SQ + SQ / 2

    def _flipped(self) -> bool:
        return self.flip if self.flip is not None else self.me == 1

    def _from_xy(self, x: float, y: float) -> int:
        c, r = int(x // SQ), int(y // SQ)
        if not (0 <= c < 8 and 0 <= r < 8):
            return -1
        if self._flipped():
            r, c = 7 - r, 7 - c
        return r * 8 + c

    def _draw_static(self) -> None:
        c = self.canvas
        c.delete("all")
        for sq in range(64):
            x, y = self._xy(sq)
            dark = (((sq >> 3) + (sq & 7)) & 1) == 1
            c.create_rectangle(x - SQ / 2, y - SQ / 2, x + SQ / 2, y + SQ / 2, fill=DARK if dark else LIGHT, outline="", tags="static")
            if dark and self.show_nums.get():
                c.create_text(x - SQ / 2 + 4, y - SQ / 2 + 3, text=str(square_number(sq)), anchor="nw", fill="#9FB1C3",
                              font=("Courier", 9), tags="static")

    def render_board(self) -> None:
        """Squares are drawn once. Only the pieces and highlights are redrawn, which keeps the board smooth."""
        sig = (self._flipped(), bool(self.show_nums.get()))
        if sig != self._static_sig:
            self._draw_static()
            self._static_sig = sig
        c = self.canvas
        c.delete("dyn")
        for sq in (self.last or ()):
            x, y = self._xy(sq)
            c.create_rectangle(x - SQ / 2, y - SQ / 2, x + SQ / 2, y + SQ / 2, fill="#6B6F45", outline="", stipple="gray50", tags="dyn")
        targets, movable = [], []
        arrow = None
        if self.mode == "play" and not self.over():
            cands = [m for m in self.legal if m.path[: len(self.sel)] == tuple(self.sel)]
            if self.sel:
                targets = {m.path[len(self.sel)] for m in cands if len(m.path) > len(self.sel)}
            elif self.has_capture() and self.rules == E.RULES_AMERICAN:
                movable = {m.path[0] for m in self.legal}
            if self.turn == self.me:
                arrow = self.an.get("best")
            else:
                arrow = self.predict[0][0] if self.predict else self.an.get("best")
        for sq in movable:
            x, y = self._xy(sq)
            c.create_oval(x - 38, y - 38, x + 38, y + 38, outline=GOLD, width=3, dash=(5, 5), tags="dyn")
        for sq in self.sel:
            x, y = self._xy(sq)
            c.create_oval(x - 37, y - 37, x + 37, y + 37, outline=GOLD, width=4, tags="dyn")
        for sq in range(64):
            v = self.board[sq]
            if v:
                x, y = self._xy(sq)
                self._piece(c, x, y, 1 if v > 0 else -1, abs(v) == 2, 33, "dyn")
        for sq in targets:
            x, y = self._xy(sq)
            c.create_oval(x - 11, y - 11, x + 11, y + 11, fill=GOLD, outline=GOLD_INK, width=2, tags="dyn")
        if arrow is not None:
            for sq in arrow.caps:
                x, y = self._xy(sq)
                c.create_oval(x - 36, y - 36, x + 36, y + 36, outline="#FF5A4D", width=3, dash=(4, 5), tags="dyn")
                c.create_line(x - 11, y - 11, x + 11, y + 11, fill="#FF5A4D", width=4, capstyle="round", tags="dyn")
                c.create_line(x + 11, y - 11, x - 11, y + 11, fill="#FF5A4D", width=4, capstyle="round", tags="dyn")
            self._arrow(arrow)

    def _arrow(self, move: E.Move) -> None:
        pts = [self._xy(s) for s in move.path]
        mine = self.turn == self.me
        colour = "#FFC93C" if mine else "#BFE0FF"
        (x0, y0), (x1, y1) = pts[0], pts[1]
        d = math.hypot(x1 - x0, y1 - y0) or 1
        pts[0] = (x0 + (x1 - x0) / d * 34, y0 + (y1 - y0) / d * 34)
        flat = [v for p in pts for v in p]
        kwargs = dict(width=9, arrow="last", arrowshape=(18, 20, 9), capstyle="round", joinstyle="round", tags="dyn")
        if not mine:
            kwargs["dash"] = (14, 8)
        self.canvas.create_line(*flat, fill="#0A1118", **{**kwargs, "width": 15, "dash": ()})
        self.canvas.create_line(*flat, fill=colour, **kwargs)

    # ------------------------------------------------------------------ side panel
    def clear_panel(self) -> None:
        for w in self.panel.winfo_children():
            w.destroy()

    def card(self, pady=(0, 10)) -> tk.Frame:
        f = tk.Frame(self.panel, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
        f.pack(fill="x", pady=pady)
        inner = tk.Frame(f, bg=PANEL)
        inner.pack(fill="x", padx=14, pady=12)
        return inner

    def eval_info(self) -> Optional[dict]:
        score = self.an.get("score")
        if score is None:
            return None
        mine = score if self.turn == self.me else -score
        if abs(score) > 9700:
            plies = 10000 - abs(score)
            return {"p": 0.985 if mine > 0 else 0.015, "forced": "win" if mine > 0 else "loss", "moves": (plies + 1) // 2,
                    "text": "Forced win" if mine > 0 else "Forced loss"}
        v = mine / 100
        p = 1 / (1 + math.exp(-mine / 170))
        text = "Even position" if abs(v) < 0.25 else (f"You lead by {abs(v):.1f} men" if v > 0 else f"You trail by {abs(v):.1f} men")
        return {"p": p, "forced": None, "moves": None, "text": text}

    def describe(self, m: E.Move) -> str:
        v = self.board[m.path[0]]
        king = abs(v) == 2
        row = m.path[-1] >> 3
        promo = (not king) and ((v > 0 and row == 7) or (v < 0 and row == 0))
        if m.caps:
            t = "Jumps one piece." if len(m.caps) == 1 else f"Jumps {len(m.caps)} pieces in a row."
        else:
            t = "King slides one square." if king else "Steps forward one square."
        return t + (" Crowns a king." if promo else "")

    def _style_row(self, parent: tk.Frame) -> None:
        row = tk.Frame(parent, bg=PANEL)
        row.pack(fill="x", pady=(8, 0))
        tk.Label(row, text="Style", bg=PANEL, fg=MUTED, font=("Helvetica", 10)).pack(side="left", padx=(0, 8))
        for key, label in (("balanced", "Balanced"), ("safe", "Safe"), ("aggressive", "Aggressive")):
            on = self.style == key
            tk.Button(row, text=label, command=lambda k=key: self._set_style(k), relief="sunken" if on else "raised",
                      bg=GOLD if on else PANEL, fg=GOLD_INK if on else INK, activebackground=GOLD, padx=10, pady=2, bd=1).pack(side="left", padx=2)
        if self.style == "balanced" and self.auto_safe and self.auto_on and self.turn == self.me:
            tk.Label(parent, text="Safe mode switched on automatically because you are behind.", bg=PANEL, fg=INK,
                     font=("Helvetica", 10, "bold"), wraplength=340, justify="left").pack(anchor="w", pady=(6, 0))
        elif self.turn == self.me and self.style != "balanced":
            tk.Label(parent, text=STYLE_HELP[self.style], bg=PANEL, fg=MUTED, font=("Helvetica", 9),
                     wraplength=340, justify="left").pack(anchor="w", pady=(4, 0))

    def _prob_rows(self, parent: tk.Frame) -> None:
        tk.Label(parent, text="Their likely replies", bg=PANEL, fg=MUTED, font=("Helvetica", 10)).pack(anchor="w", pady=(8, 2))
        for mv, p in self.predict[:3]:
            row = tk.Frame(parent, bg=PANEL)
            row.pack(fill="x", pady=1)
            tk.Label(row, text=E.notation(mv), font=("Courier", 12, "bold"), bg=PANEL, fg=INK, width=10, anchor="w").pack(side="left")
            bar = tk.Canvas(row, width=150, height=12, bg=LINE, highlightthickness=0)
            bar.pack(side="left", padx=(0, 8))
            bar.create_rectangle(0, 0, 150 * p, 12, fill=GOLD, outline="")
            tk.Label(row, text=f"{p:.0%}", bg=PANEL, fg=INK, font=("Helvetica", 10, "bold")).pack(side="left")

    def _forced_banner(self, c: tk.Frame, ev: dict) -> None:
        n = ev["moves"]
        plural = "" if n == 1 else "s"
        if ev["forced"] == "win":
            banner = f"Forced win in {n} move{plural}"
            sub = "Whatever they play, they cannot avoid it. Time to offer a handshake."
            bg, fg = GOLD, GOLD_INK
        else:
            banner = f"They have a forced win in {n} move{plural}"
            sub = "Look for the longest defence, or switch to Safe and hope for a slip."
            bg, fg = LINE, INK
        box = tk.Frame(c, bg=bg)
        box.pack(fill="x", pady=(10, 0))
        tk.Label(box, text=banner, bg=bg, fg=fg, font=("Helvetica", 13, "bold"), padx=10, pady=4, anchor="w").pack(fill="x")
        tk.Label(box, text=sub, bg=bg, fg=fg, font=("Helvetica", 9), padx=10, pady=4, wraplength=320,
                 justify="left", anchor="w").pack(fill="x")

    def render_panel(self) -> None:
        self._panel_dirty = False
        self._panel_at = time.monotonic()
        self.swap_btn.config(text=f"You play {name(self.me)} · swap")
        self.rules_label.config(text=E.RULE_NAMES[self.rules])
        self.engine_label.config(text=f"Engine: {E.engine_name()} · {self.threads} thread" + ("" if self.threads == 1 else "s"))
        self.clear_panel()
        if self.show_settings:
            self._render_settings()
            return
        if self.mode == "setup":
            self._render_setup()
            return
        mine = self.turn == self.me
        a = self.an
        if self.over():
            win = self.turn != self.me
            c = self.card()
            tk.Label(c, text="You win" if win else "You lose", font=("Helvetica", 18, "bold"), bg=PANEL, fg=INK).pack(anchor="w")
            tk.Label(c, text=f"{name(self.turn)} has no legal move.", bg=PANEL, fg=MUTED).pack(anchor="w")
        else:
            c = self.card()
            head = tk.Frame(c, bg=PANEL)
            head.pack(fill="x")
            tk.Label(head, text="Your best move" if mine else "Their move", font=("Helvetica", 14, "bold"), bg=PANEL, fg=INK).pack(side="left")
            status = ""
            if a.get("status") == "thinking":
                status = ("Looking for a forced win" if a.get("prove") else "Searching") + f" · depth {a.get('depth', 0)}"
            elif a.get("status") == "done":
                status = f"Searched {a.get('depth')} moves ahead"
            tk.Label(head, text=status, bg=PANEL, fg=MUTED, font=("Helvetica", 10)).pack(side="right")
            if mine:
                self._style_row(c)
            best = a.get("best")
            shown = best if (mine or not self.predict) else self.predict[0][0]
            if shown:
                tk.Label(c, text=E.notation(shown), font=("Courier", 30, "bold"), bg=PANEL, fg=INK if mine else MUTED).pack(anchor="w", pady=(6, 0))
                if mine:
                    tk.Label(c, text=("Only legal move. " if a.get("forced") else "") + self.describe(shown), bg=PANEL, fg=MUTED).pack(anchor="w")
                elif self.predict:
                    tk.Label(c, text=f"Most likely, about {self.predict[0][1]:.0%} sure.", bg=PANEL, fg=MUTED).pack(anchor="w")
                else:
                    tk.Label(c, text="Their strongest reply so far.", bg=PANEL, fg=MUTED).pack(anchor="w")
            else:
                tk.Label(c, text="...", font=("Courier", 30, "bold"), bg=PANEL, fg=MUTED).pack(anchor="w", pady=(6, 0))
                tk.Label(c, text="Looking at the position.", bg=PANEL, fg=MUTED).pack(anchor="w")
            if not mine and self.predict:
                self._prob_rows(c)
                if best and best.path != self.predict[0][0].path:
                    tk.Label(c, text=f"Their strongest reply would be {E.notation(best)}.", bg=PANEL, fg=MUTED,
                             font=("Helvetica", 10)).pack(anchor="w", pady=(6, 0))
            if self.has_capture():
                if self.rules == E.RULES_AMERICAN:
                    txt = "A capture is available, so it is mandatory for " + ("you." if mine else "them.")
                else:
                    txt = "A capture is available. Skipping it is allowed."
                tk.Label(c, text=txt, bg=PANEL, fg=INK, font=("Helvetica", 10)).pack(anchor="w", pady=(8, 0))
            if self.msg:
                tk.Label(c, text=self.msg, bg=PANEL, fg=INK, font=("Helvetica", 10), wraplength=340, justify="left").pack(anchor="w", pady=(6, 0))
            ev = self.eval_info()
            if ev:
                if ev["forced"]:
                    self._forced_banner(c, ev)
                elif a.get("prove") and a.get("status") == "done":
                    tk.Label(c, text=f"No forced win found within {a.get('depth')} moves. It may still be winning.", bg=PANEL, fg=INK,
                             font=("Helvetica", 10), wraplength=340, justify="left").pack(anchor="w", pady=(10, 0))
                bar = tk.Canvas(c, width=340, height=10, bg=RED_DISC if self.me > 0 else BLACK_DISC, highlightthickness=1, highlightbackground=LINE)
                bar.pack(anchor="w", pady=(12, 4))
                bar.create_rectangle(0, 0, 340 * ev["p"], 12, fill=BLACK_DISC if self.me > 0 else RED_DISC, outline="")
                row = tk.Frame(c, bg=PANEL)
                row.pack(fill="x")
                label = ev["text"] if ev["forced"] else f"{ev['text']} · about {ev['p']:.0%} for you"
                tk.Label(row, text=label, bg=PANEL, fg=INK, font=("Helvetica", 11, "bold")).pack(side="left")
                tk.Label(row, text=f"{a.get('nodes', 0):,} positions", bg=PANEL, fg=MUTED, font=("Courier", 10)).pack(side="right")
            pv = a.get("pv") or []
            if len(pv) > 1:
                side = self.turn
                parts = []
                for m in pv:
                    parts.append(f"{'●' if side > 0 else '○'} {E.notation(m)}")
                    side = -side
                tk.Label(c, text="Likely line:  " + "   ".join(parts), bg=PANEL, fg=MUTED, wraplength=340, justify="left",
                         font=("Courier", 10)).pack(anchor="w", pady=(10, 0))
            alts = a.get("alts") or []
            if self.show_alts and mine and len(alts) > 1 and best:
                scored = [(x, x.score) for x in alts if x.score is not None]
                ranked = sorted(scored, key=lambda t: -t[1])[:4]
                lines = []
                for x, sc in ranked:
                    lines.append(f"{E.notation(x.move):<14}{sc / 100:+.2f}" + ("   <- chosen" if x.move.path == best.path else ""))
                tk.Label(c, text="Other options (quick look, in men)", bg=PANEL, fg=MUTED, font=("Helvetica", 10)).pack(anchor="w", pady=(10, 0))
                tk.Label(c, text="\n".join(lines), bg=PANEL, fg=INK, font=("Courier", 10), justify="left").pack(anchor="w")
            row = tk.Frame(c, bg=PANEL)
            row.pack(fill="x", pady=(12, 0))
            ttk.Button(row, text="Play this move" if mine else "Play their likely move", command=self.apply_best,
                       state="normal" if shown else "disabled").pack(side="left")
            ttk.Button(row, text="Recalculate", command=self._recalc).pack(side="left", padx=6)
            ttk.Button(row, text="Find forced win", command=self._prove).pack(side="left")
            if not mine or self.pred_log:
                self._prediction_footer(c)
        c = self.card()
        head = tk.Frame(c, bg=PANEL)
        head.pack(fill="x")
        tk.Label(head, text="Moves", font=("Helvetica", 12, "bold"), bg=PANEL, fg=INK).pack(side="left")
        ttk.Button(head, text="Undo", command=self.undo, state="normal" if self.hist else "disabled").pack(side="right")
        if self.hist:
            text = "   ".join(f"{'●' if e['side'] > 0 else '○'} {e['note']}" for e in self.hist)
        else:
            text = f"Click a piece, then its destination, to record what was played on the real board. {name(self.turn)} is to move."
        tk.Label(c, text=text, bg=PANEL, fg=MUTED, wraplength=340, justify="left", font=("Courier", 10) if self.hist else None).pack(anchor="w", pady=(6, 0))

    def _prediction_footer(self, c: tk.Frame) -> None:
        txt = self.model.describe()
        if self.pred_log:
            log = "  ".join("✓" if h else "✗" for h in self.pred_log[-10:])
            hits = sum(1 for h in self.pred_log if h)
            txt = f"Predictions this game: {log}   ({hits} of {len(self.pred_log)})\n" + txt
        tk.Label(c, text=txt, bg=PANEL, fg=MUTED, font=("Helvetica", 9), wraplength=340, justify="left").pack(anchor="w", pady=(10, 0))

    def _render_setup(self) -> None:
        c = self.card()
        me = self.me
        tk.Label(c, text="Place pieces", font=("Helvetica", 14, "bold"), bg=PANEL, fg=INK).pack(anchor="w")
        self._v_brush = tk.StringVar(value=self.brush)
        for key, label in (("meMan", "My man"), ("meKing", "My king"), ("oppMan", "Their man"), ("oppKing", "Their king"), ("erase", "Erase")):
            tk.Radiobutton(c, text=label, value=key, variable=self._v_brush, bg=PANEL, fg=INK, activebackground=PANEL,
                           selectcolor=PANEL, command=lambda: setattr(self, "brush", self._v_brush.get())).pack(anchor="w")
        tk.Label(c, text="Who moves next", font=("Helvetica", 12, "bold"), bg=PANEL, fg=INK).pack(anchor="w", pady=(12, 0))
        self.turn_var = tk.IntVar(value=self.turn)
        for side, label in ((me, f"You · {name(me)}"), (-me, f"Opponent · {name(-me)}")):
            tk.Radiobutton(c, text=label, value=side, variable=self.turn_var, bg=PANEL, fg=INK, activebackground=PANEL,
                           selectcolor=PANEL, command=lambda: setattr(self, "turn", self.turn_var.get())).pack(anchor="w")
        nb = sum(1 for v in self.board if v > 0)
        nr = sum(1 for v in self.board if v < 0)
        tk.Label(c, text=f"Black {nb} · Red {nr}", bg=PANEL, fg=MUTED).pack(anchor="w", pady=(10, 0))
        row = tk.Frame(c, bg=PANEL)
        row.pack(fill="x", pady=8)
        ttk.Button(row, text="Starting position", command=lambda: self._set_board(E.start_board(), self.first)).pack(side="left")
        ttk.Button(row, text="Clear board", command=lambda: self._set_board([0] * 64, self.turn)).pack(side="left", padx=6)
        ttk.Button(c, text="Analyze this position", command=lambda: self.set_mode("play")).pack(fill="x")
        tk.Label(c, text="Click a dark square to place the selected piece. Click it again to remove it.",
                 bg=PANEL, fg=MUTED, wraplength=340, justify="left", font=("Helvetica", 10)).pack(anchor="w", pady=(8, 0))

    def _set_board(self, board: List[int], turn: int) -> None:
        self.board, self.turn = board, turn
        self.render()

    # ------------------------------------------------------------------ settings panel
    def _settings_changed(self) -> None:
        try:
            self.style = self._v_style.get()
            self.auto_safe = bool(self._v_auto.get())
            self.auto_margin = round(float(self._v_margin.get()), 1)
            self.threads = min(32, max(1, int(self._v_threads.get())))
            self.show_alts = bool(self._v_alts.get())
            self.first = int(self._v_first.get())
            new_rules = int(self._v_rules.get())
        except (ValueError, tk.TclError):
            return
        if new_rules != self.rules:
            self.sel = []
        self.rules = new_rules
        self.auto_on = False
        self._save_settings()
        self.rules_label.config(text=E.RULE_NAMES[self.rules])
        if self.mode == "play":
            self.refresh_legal()
            self.request()
            self.render_board()

    def _toggle_settings(self) -> None:
        self.show_settings = not self.show_settings
        self.render_panel()

    def _render_settings(self) -> None:
        from . import native
        c = self.card()
        tk.Label(c, text="Settings", font=("Helvetica", 14, "bold"), bg=PANEL, fg=INK).pack(anchor="w")
        tk.Label(c, text="Changes apply straight away, even in the middle of a game.", bg=PANEL, fg=MUTED,
                 font=("Helvetica", 10)).pack(anchor="w", pady=(2, 8))
        self._v_rules = tk.IntVar(value=self.rules)
        self._v_first = tk.IntVar(value=self.first)
        self._v_style = tk.StringVar(value=self.style)
        tk.Label(c, text="Game type", font=("Helvetica", 11, "bold"), bg=PANEL, fg=INK).pack(anchor="w")
        for val in (E.RULES_AMERICAN, E.RULES_OPTIONAL):
            tk.Radiobutton(c, text=E.RULE_NAMES[val], value=val, variable=self._v_rules, command=self._settings_changed, bg=PANEL, fg=INK,
                           activebackground=PANEL, selectcolor=PANEL, anchor="w").pack(anchor="w")
            tk.Label(c, text=RULE_HELP[val], bg=PANEL, fg=MUTED, font=("Helvetica", 9), wraplength=330, justify="left").pack(anchor="w", padx=(24, 0))
        tk.Label(c, text="Who moves first in a new game", font=("Helvetica", 11, "bold"), bg=PANEL, fg=INK).pack(anchor="w", pady=(10, 0))
        row = tk.Frame(c, bg=PANEL)
        row.pack(anchor="w")
        for val, label in ((1, "Black"), (-1, "Red")):
            tk.Radiobutton(row, text=label, value=val, variable=self._v_first, command=self._settings_changed, bg=PANEL, fg=INK,
                           activebackground=PANEL, selectcolor=PANEL).pack(side="left", padx=(0, 12))
        tk.Label(c, text="Playing style", font=("Helvetica", 11, "bold"), bg=PANEL, fg=INK).pack(anchor="w", pady=(10, 0))
        for key, label in (("balanced", "Balanced"), ("safe", "Safe (take no chances)"), ("aggressive", "Aggressive")):
            tk.Radiobutton(c, text=label, value=key, variable=self._v_style, command=self._settings_changed, bg=PANEL, fg=INK,
                           activebackground=PANEL, selectcolor=PANEL, anchor="w").pack(anchor="w")
            tk.Label(c, text=STYLE_HELP[key], bg=PANEL, fg=MUTED, font=("Helvetica", 9), wraplength=330, justify="left").pack(anchor="w", padx=(24, 0))
        self._v_auto = tk.BooleanVar(value=self.auto_safe)
        tk.Checkbutton(c, text="Switch to Safe automatically when I am behind", variable=self._v_auto, command=self._settings_changed,
                       bg=PANEL, fg=INK, activebackground=PANEL, selectcolor=PANEL, anchor="w").pack(anchor="w", pady=(10, 0))
        row = tk.Frame(c, bg=PANEL)
        row.pack(fill="x", padx=(24, 0))
        tk.Label(row, text="when I trail by (men)", bg=PANEL, fg=MUTED, font=("Helvetica", 10)).pack(side="left")
        self._v_margin = tk.DoubleVar(value=self.auto_margin)
        tk.Scale(row, from_=0.2, to=2.0, resolution=0.1, orient="horizontal", variable=self._v_margin, length=150, bg=PANEL,
                 highlightthickness=0, command=lambda _v: self._settings_changed()).pack(side="left", padx=8)
        self._v_alts = tk.BooleanVar(value=self.show_alts)
        tk.Checkbutton(c, text="Show other options with their scores", variable=self._v_alts, command=self._settings_changed,
                       bg=PANEL, fg=INK, activebackground=PANEL, selectcolor=PANEL, anchor="w").pack(anchor="w", pady=(8, 0))
        row = tk.Frame(c, bg=PANEL)
        row.pack(fill="x", pady=(8, 0))
        tk.Label(row, text="CPU threads", bg=PANEL, fg=INK).pack(side="left")
        self._v_threads = tk.IntVar(value=self.threads)
        tk.Spinbox(row, from_=1, to=max(1, native.hardware_threads()), width=4, textvariable=self._v_threads,
                   command=self._settings_changed).pack(side="left", padx=8)
        tk.Label(row, text=f"of {native.hardware_threads()} available", bg=PANEL, fg=MUTED, font=("Helvetica", 9)).pack(side="left")
        row = tk.Frame(c, bg=PANEL)
        row.pack(fill="x", pady=(10, 0))
        ttk.Button(row, text="Forget what I learned about my opponent", command=self._reset_model).pack(side="left")
        if not native.available():
            tk.Label(c, text="The C engine is not built, so Safe, Aggressive and extra threads have no effect. Run build_native.py.",
                     bg=PANEL, fg=INK, wraplength=330, justify="left", font=("Helvetica", 10)).pack(anchor="w", pady=(10, 0))
        ttk.Button(c, text="Done", command=self._toggle_settings).pack(anchor="w", pady=(12, 0))

    def _reset_model(self) -> None:
        self.model.reset()
        self.pred_log = []
        self.predict = []
        self._save_settings()
        self.render_board()

    # ------------------------------------------------------------------ game logic
    def render(self) -> None:
        self.render_board()
        self.render_panel()

    def refresh_legal(self) -> None:
        self.legal = E.legal_moves(self.board, self.turn, self.rules) if self.mode == "play" else []

    def over(self) -> bool:
        return self.mode == "play" and not self.legal

    def has_capture(self) -> bool:
        return bool(self.legal) and bool(self.legal[0].caps)

    def do_move(self, m: E.Move) -> None:
        mover = self.turn
        if mover != self.me:
            if self.pending is not None and self.pending[0] == tuple(self.board):
                hit = self.model.observe(self.pending[1], m.path)
                if hit is not None:
                    self.pred_log.append(hit)
                    self._save_settings()
            elif len(self.legal) == 1:
                self.pred_log.append(True)
        irrev = bool(m.caps) or abs(self.board[m.path[0]]) == 1
        self.hist.append({"board": self.board, "turn": self.turn, "side": self.turn, "note": E.notation(m),
                          "last": self.last, "irrev": irrev})
        self.board = E.apply_move(self.board, m)
        self.turn = -self.turn
        self.last = m.path
        self.sel, self.msg = [], ""
        self.predict, self.pending = [], None
        self.refresh_legal()
        self.request()
        self.render()

    def undo(self) -> None:
        if not self.hist or self.mode != "play":
            return
        e = self.hist.pop()
        self.board, self.turn, self.last = e["board"], e["turn"], e["last"]
        self.sel, self.msg = [], ""
        self.predict, self.pending = [], None
        self.refresh_legal()
        self.request()
        self.render()

    def apply_best(self) -> None:
        pick = self.an.get("best") if (self.turn == self.me or not self.predict) else self.predict[0][0]
        if not pick:
            return
        for m in self.legal:
            if m.path == pick.path:
                self.do_move(m)
                return

    def _on_click(self, ev: tk.Event) -> None:
        sq = self._from_xy(ev.x, ev.y)
        if sq < 0 or (((sq >> 3) + (sq & 7)) & 1) == 0:
            return
        if self.mode == "play":
            self._play_click(sq)
        else:
            self._setup_click(sq)

    def _play_click(self, sq: int) -> None:
        if self.over():
            return
        self.msg = ""
        sel = tuple(self.sel)
        cands = [m for m in self.legal if m.path[: len(sel)] == sel]
        if not sel:
            if any(m.path[0] == sq for m in self.legal):
                self.sel = [sq]
            elif self.board[sq] and (self.board[sq] > 0) == (self.turn > 0):
                self.msg = ("You must capture. Pick a piece with a dashed ring."
                            if self.has_capture() and self.rules == E.RULES_AMERICAN else "That piece has no legal move.")
            elif self.board[sq]:
                self.msg = f"{name(self.turn)} is to move."
        else:
            nxt = [m for m in cands if len(m.path) > len(sel) and m.path[len(sel)] == sq]
            if nxt:
                self.sel.append(sq)
                done = [m for m in self.legal if m.path == tuple(self.sel)]
                if done:
                    self.do_move(done[0])
                    return
            elif len(sel) == 1 and any(m.path[0] == sq for m in self.legal):
                self.sel = [sq]
            else:
                self.sel = []
        self.render()

    def _setup_click(self, sq: int) -> None:
        me = self.me
        v = {"meMan": me, "meKing": 2 * me, "oppMan": -me, "oppKing": -2 * me, "erase": 0}[self.brush]
        row = sq >> 3
        if abs(v) == 1 and ((v > 0 and row == 7) or (v < 0 and row == 0)):
            v *= 2                       # a man on the far row is a king
        self.board = list(self.board)
        self.board[sq] = 0 if self.board[sq] == v else v
        self.render()

    def set_mode(self, mode: str) -> None:
        if mode == self.mode:
            return
        self.mode, self.sel, self.msg = mode, [], ""
        if mode == "setup":
            self._stop()
            self.req += 1
            self.an, self.legal, self.predict, self.pending = {}, [], [], None
        else:
            self.hist, self.last = [], None
            self.auto_on = False
            self.pred_log = []
            self.refresh_legal()
            self.request()
        self.render()

    def _new_game_click(self) -> None:
        """Two clicks within three seconds, so a stray click cannot throw away a game."""
        now = time.monotonic()
        if now - self.new_armed > 3.0:
            self.new_armed = now
            self.new_btn.config(text="Click again to confirm")
            self.after(3000, lambda: self.new_btn.config(text="New game"))
            return
        self.new_armed = 0.0
        self.new_btn.config(text="New game")
        self.new_game()

    def new_game(self) -> None:
        self._stop()
        self.board, self.turn = E.start_board(), self.first
        self.hist, self.last, self.sel, self.msg = [], None, [], ""
        self.predict, self.pending, self.pred_log = [], None, []
        self.auto_on = False
        self.mode, self.show_settings = "play", False
        self.flip = None
        self.refresh_legal()
        self.request()
        self.render()

    def _swap(self) -> None:
        self.me = -self.me
        self.flip, self.sel = None, []
        self.predict, self.pending, self.pred_log = [], None, []
        if self.mode == "play":
            self.request()
        self.render()

    def _flip(self) -> None:
        self.flip = not self._flipped()
        self.render_board()

    def _recalc(self) -> None:
        self.request()
        self.render()

    def _prove(self) -> None:
        self.request(prove=True)
        self.render()

    def _set_style(self, key: str) -> None:
        self.style = key
        self.auto_on = False
        self._save_settings()
        if self.mode == "play":
            self.request()
        self.render()

    def _on_think(self, _e=None) -> None:
        self.think_ms = THINK_CHOICES[self.think_box.get()]
        self._save_settings()
        if self.mode == "play":
            self.request()
            self.render_panel()

    # ------------------------------------------------------------------ background search
    def _stop(self) -> None:
        if self.stop_ev:
            self.stop_ev.set()

    def request(self, prove: bool = False) -> None:
        self._stop()
        self.req += 1
        rid = self.req
        self.predict = []
        if self.mode != "play":
            self.an = {}
            return
        self.refresh_legal()
        if not self.legal:
            self.an = {"status": "over"}
            return
        style = "balanced" if prove else self.effective_style()
        self.an = {"status": "thinking", "depth": 0, "nodes": 0, "score": None, "best": None, "pv": [], "style": style, "prove": prove}
        if prove:
            ms, threads = 45000, max(self.threads, 1)
        else:
            ms, threads = (self.think_ms if self.turn == self.me else min(self.think_ms, 3000)), self.threads
        ev = threading.Event()
        self.stop_ev = ev
        board, side = list(self.board), self.turn
        hm, boards = self.reversible_history()
        opts = E.Options.for_style(STYLE_INDEX[style], threads, hm, self.show_alts or self.turn != self.me, self.rules)

        def work() -> None:
            res = E.analyze(board, side, ms,
                            on_progress=lambda p: self.q.put((rid, "progress", p)),
                            stop=ev.is_set, options=opts, history=boards)
            self.q.put((rid, "done", res))

        threading.Thread(target=work, daemon=True).start()

    def _finish_analysis(self, data) -> bool:
        """Store a finished search. Returns True when the app should search again."""
        used = self.an.get("style", "balanced")
        prove = self.an.get("prove", False)
        self.an = {"status": "done", "depth": data.depth, "nodes": data.nodes, "score": data.score,
                   "best": data.best, "pv": data.pv, "forced": data.n_moves == 1,
                   "alts": data.alts, "style": used, "prove": prove}
        if self.turn != self.me:
            if len(self.legal) == 1:
                self.predict = [(self.legal[0], 1.0)]
            elif data.alts:
                self.predict = self.model.predict(data.alts)
                self.pending = (tuple(self.board), data.alts)
        # Only a balanced search gives an honest score, so only it can switch auto-safe on or off.
        if self.auto_safe and used == "balanced" and not prove and data.best is not None:
            mine = data.score if self.turn == self.me else -data.score
            if self._update_auto(mine) and self.turn == self.me and self.style == "balanced":
                return True
        return False

    def _poll(self) -> None:
        rerun = False
        try:
            while True:
                rid, kind, data = self.q.get_nowait()
                if rid != self.req:
                    continue
                if kind == "progress":
                    self.an.update(depth=data["depth"], score=data["score"], best=data["best"], nodes=data["nodes"], pv=[])
                else:
                    rerun = self._finish_analysis(data)
                    self._panel_at = 0.0          # a finished search is always drawn straight away
                self._panel_dirty = True
        except queue.Empty:
            pass
        if rerun and self.mode == "play":
            self.request()
        if self._panel_dirty and self.mode == "play":
            if time.monotonic() - self._panel_at >= PANEL_REFRESH:
                if self.show_settings:
                    self._panel_dirty = False
                    self.render_board()
                else:
                    self.render()
        self.after(100, self._poll)


def main() -> None:
    App().mainloop()


if __name__ == "__main__":
    main()
