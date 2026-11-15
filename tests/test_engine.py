import unittest

from kingmaker import engine as E


def empty():
    return [0] * 64


def sq(n):
    """Standard 1-32 square number -> 64-square index."""
    return E.sq64(n - 1)


class PerftTests(unittest.TestCase):
    def perft(self, b, side, d):
        mv = E.legal_moves(b, side)
        if d == 1:
            return len(mv)
        return sum(self.perft(E.apply_move(b, m), -side, d - 1) for m in mv)

    def test_known_node_counts(self):
        expected = [7, 49, 302, 1469, 7361, 36768]
        for d, n in enumerate(expected, start=1):
            self.assertEqual(self.perft(E.start_board(), 1, d), n, f"depth {d}")


class NativeTests(unittest.TestCase):
    def setUp(self):
        from kingmaker import native
        if not native.available():
            self.skipTest("native library not built (run python build_native.py)")
        self.native = native

    def test_native_perft(self):
        expected = [7, 49, 302, 1469, 7361, 36768, 179740, 845931]
        for d, n in enumerate(expected, start=1):
            self.assertEqual(self.native.perft(E.start_board(), 1, d), n, f"depth {d}")

    def test_native_matches_python_on_tactics(self):
        b = empty()
        b[sq(14)], b[sq(18)], b[sq(30)] = 1, -1, -1
        self.assertEqual(E.notation(E.analyze(b, 1, 300).best), "14x23")
        a = E.analyze(E.start_board(), 1, 300)
        self.assertGreaterEqual(a.depth, 8)

    def test_native_handles_no_moves(self):
        b = empty()
        b[sq(1)] = 1
        self.assertIsNone(E.analyze(b, -1, 50).best)


class NativeFeatureTests(unittest.TestCase):
    def setUp(self):
        from kingmaker import native
        if not native.available():
            self.skipTest("native library not built (run python build_native.py)")

    def kings_only(self):
        b = empty()
        b[sq(1)], b[sq(32)] = 2, -2      # one king each, far apart
        return b

    def test_eighty_ply_rule_is_a_draw(self):
        opts = E.Options(style=0, draw_value=123, threads=1, halfmove=79)
        a = E.analyze(self.kings_only(), 1, 300, options=opts)
        self.assertEqual(a.score, 123)

    def test_draw_value_steers_the_engine(self):
        seek = E.analyze(self.kings_only(), 1, 400, options=E.Options(0, 500, 1, 0))
        self.assertGreater(seek.score, 300, "with a high draw value the engine should head for a repetition")

    def test_styles_return_legal_moves(self):
        b = E.start_board()
        legal = {m.path for m in E.legal_moves(b, 1)}
        for style in (E.STYLE_BALANCED, E.STYLE_SAFE, E.STYLE_AGGRESSIVE):
            a = E.analyze(b, 1, 400, options=E.Options.for_style(style))
            self.assertIn(a.best.path, legal)

    def test_threads_and_alternatives(self):
        b = E.start_board()
        a = E.analyze(b, 1, 500, options=E.Options.for_style(0, threads=2, want_alts=True))
        self.assertIn(a.best.path, {m.path for m in E.legal_moves(b, 1)})
        self.assertEqual(len(a.alts), len(E.legal_moves(b, 1)))
        self.assertTrue(all(x.score is not None for x in a.alts))

    def test_history_is_accepted(self):
        b = E.start_board()
        a = E.analyze(b, 1, 200, options=E.Options.for_style(0, 1, 2), history=[b, b])
        self.assertIsNotNone(a.best)


class OptionalCaptureTests(unittest.TestCase):
    def position(self):
        b = empty()
        b[sq(14)], b[sq(18)], b[sq(1)] = 1, -1, 1     # black 14 can jump red 18, black 1 could just step
        return b

    def test_american_forces_the_jump(self):
        notes = {E.notation(m) for m in E.legal_moves(self.position(), 1, E.RULES_AMERICAN)}
        self.assertEqual(notes, {"14x23"})

    def test_optional_allows_skipping_the_jump(self):
        notes = {E.notation(m) for m in E.legal_moves(self.position(), 1, E.RULES_OPTIONAL)}
        self.assertIn("14x23", notes)
        self.assertTrue(any("-" in n for n in notes), "quiet moves must be legal when a jump is optional")

    def test_optional_chain_is_finished(self):
        b = empty()
        b[sq(9)], b[sq(14)], b[sq(22)] = 1, -1, -1
        notes = {E.notation(m) for m in E.legal_moves(b, 1, E.RULES_OPTIONAL)}
        self.assertIn("9x18x25", notes)
        self.assertNotIn("9x18", notes)

    def test_both_engines_play_legal_optional_moves(self):
        b = self.position()
        legal = {m.path for m in E.legal_moves(b, 1, E.RULES_OPTIONAL)}
        self.assertIn(E.analyze_py(b, 1, 200, rules=E.RULES_OPTIONAL).best.path, legal)
        from kingmaker import native
        if native.available():
            a = E.analyze(b, 1, 300, options=E.Options.for_style(0, 1, 0, False, E.RULES_OPTIONAL))
            self.assertIn(a.best.path, legal)

    def test_optional_engine_beats_random(self):
        from kingmaker import native
        if not native.available():
            self.skipTest("native library not built")
        import random
        rng = random.Random(3)
        b, side = E.start_board(), 1
        for _ in range(160):
            moves = E.legal_moves(b, side, E.RULES_OPTIONAL)
            if not moves:
                break
            if side == 1:
                m = E.analyze(b, side, 50, options=E.Options.for_style(0, 1, 0, False, E.RULES_OPTIONAL)).best
            else:
                m = rng.choice(moves)
            b, side = E.apply_move(b, m), -side
        self.assertEqual(E.legal_moves(b, side, E.RULES_OPTIONAL), [])
        self.assertEqual(side, -1)


class OpponentModelTests(unittest.TestCase):
    def alts(self, deep_scores):
        moves = E.legal_moves(E.start_board(), 1)[: len(deep_scores)]
        return [E.AltInfo(m, (s, s, s)) for m, s in zip(moves, deep_scores)]

    def test_prediction_favours_the_high_scoring_move(self):
        from kingmaker.opponent import OpponentModel
        pred = OpponentModel().predict(self.alts([50, 0, -40]))
        self.assertEqual(pred[0][0].path, self.alts([50, 0, -40])[0].move.path)
        self.assertAlmostEqual(sum(p for _m, p in pred), 1.0, places=6)

    def test_model_learns_a_player_who_ignores_the_engine(self):
        from kingmaker.opponent import OpponentModel
        model = OpponentModel()
        alts = self.alts([60, 40, 20, 0])
        for _ in range(12):                      # this player always picks the worst-scored move
            model.observe(alts, alts[3].move.path)
        best_after = model.predict(alts)[0][0].path
        fresh = OpponentModel().predict(alts)[0][0].path
        self.assertEqual(fresh, alts[0].move.path)
        self.assertNotEqual(model.predict(alts)[0][1], OpponentModel().predict(alts)[0][1])
        self.assertTrue(model.moves_seen == 12)
        self.assertIsNotNone(best_after)

    def test_state_round_trips(self):
        from kingmaker.opponent import OpponentModel
        m = OpponentModel()
        m.observe(self.alts([10, 0]), self.alts([10, 0])[0].move.path)
        again = OpponentModel(m.to_dict())
        self.assertEqual(again.ll, {k: round(v, 4) for k, v in m.ll.items()})


class RuleTests(unittest.TestCase):
    def test_capture_is_mandatory(self):
        b = empty()
        b[sq(14)], b[sq(18)], b[sq(1)] = 1, -1, 1   # black 14 can jump red 18; black 1 could step
        moves = E.legal_moves(b, 1)
        self.assertTrue(moves and all(m.caps for m in moves))

    def test_multi_jump(self):
        b = empty()
        b[sq(9)], b[sq(14)], b[sq(22)] = 1, -1, -1
        moves = E.legal_moves(b, 1)
        self.assertEqual([E.notation(m) for m in moves], ["9x18x25"])
        self.assertEqual(len(moves[0].caps), 2)

    def test_man_stops_when_crowned(self):
        b = empty()
        b[sq(23)], b[sq(27)] = 1, -1                # black man jumps into row 7 and must stop there
        after = E.apply_move(b, E.legal_moves(b, 1)[0])
        self.assertEqual(after[sq(32)], 2)

    def test_men_do_not_move_backwards(self):
        b = empty()
        b[sq(18)] = 1
        dests = {E.sq32(m.path[-1]) + 1 for m in E.legal_moves(b, 1)}
        self.assertEqual(dests, {22, 23})

    def test_kings_move_both_ways(self):
        b = empty()
        b[sq(18)] = 2
        dests = {E.sq32(m.path[-1]) + 1 for m in E.legal_moves(b, 1)}
        self.assertEqual(dests, {14, 15, 22, 23})

    def test_no_moves_means_loss(self):
        b = empty()
        b[sq(1)] = 1
        self.assertEqual(E.legal_moves(b, -1), [])
        self.assertIsNone(E.analyze(b, -1, 50).best)


class SearchTests(unittest.TestCase):
    def test_takes_a_free_piece(self):
        b = empty()
        b[sq(14)], b[sq(18)], b[sq(30)] = 1, -1, -1
        a = E.analyze(b, 1, 500)
        self.assertEqual(E.notation(a.best), "14x23")

    def test_finds_forced_win(self):
        b = empty()
        b[sq(10)] = 2
        b[sq(30)] = -1
        a = E.analyze(b, 1, 1000)
        self.assertGreater(a.score, 0)

    def test_beats_random_player(self):
        import random
        rng = random.Random(7)
        b, side = E.start_board(), 1
        for _ in range(120):
            moves = E.legal_moves(b, side)
            if not moves:
                break
            m = E.analyze(b, side, 60).best if side == 1 else rng.choice(moves)
            b, side = E.apply_move(b, m), -side
        self.assertEqual(E.legal_moves(b, side), [])
        self.assertEqual(side, -1, "black (engine) should win")


if __name__ == "__main__":
    unittest.main()
