"""SC / 弹幕搜索纯函数（ROADMAP 118）的离线测试。"""

import unittest

from blive_sc_get.text_search import (
    ScSearchHit,
    dm_line_matches,
    dm_match_indices,
    iter_sc_hits,
    normalize_query,
    sc_hit_from_record,
    sc_hit_tooltip,
    sc_preview,
    sc_record_matches,
    sc_record_text,
    search_counter,
    step_index,
)


def _record(index: int, uname: str = "观众", message: str = "你好") -> dict:
    return {
        "time_received": f"2026-10-25 12:00:{index:02d}",
        "sc": {
            "id": 1000 + index,
            "user": {"uname": uname},
            "message": message,
            "price": 30,
        },
    }


class NormalizeQueryTests(unittest.TestCase):
    def test_strip_and_casefold(self):
        self.assertEqual(normalize_query("  Hello  "), "hello")
        self.assertEqual(normalize_query(None), "")
        self.assertEqual(normalize_query(123), "123")

    def test_chinese_unchanged(self):
        self.assertEqual(normalize_query("醒目留言"), "醒目留言")


class ScRecordMatchTests(unittest.TestCase):
    def test_matches_message_user_and_time(self):
        record = _record(1, uname="小明", message="今天天气不错")
        self.assertTrue(sc_record_matches(record, "天气"))
        self.assertTrue(sc_record_matches(record, "小明"))
        self.assertTrue(sc_record_matches(record, "12:00:01"))

    def test_case_insensitive(self):
        record = _record(1, message="Hello World")
        self.assertTrue(sc_record_matches(record, "hello"))
        self.assertTrue(sc_record_matches(record, "WORLD"))

    def test_empty_query_never_matches(self):
        record = _record(1)
        self.assertFalse(sc_record_matches(record, ""))
        self.assertFalse(sc_record_matches(record, "   "))

    def test_text_joins_fields(self):
        self.assertIn("小明", sc_record_text(_record(1, uname="小明")))
        self.assertIn("你好", sc_record_text(_record(1)))

    def test_broken_record(self):
        self.assertFalse(sc_record_matches(None, "x"))
        self.assertEqual(sc_record_text("bad"), "")


class ScPreviewTests(unittest.TestCase):
    def test_snippet_around_hit(self):
        message = "前" * 100 + "关键词" + "后" * 100
        preview = sc_preview(_record(1, message=message), "关键词", limit=40)
        self.assertIn("关键词", preview)
        self.assertLessEqual(len(preview), 42)  # 两侧省略号
        self.assertTrue(preview.startswith("…"))
        self.assertTrue(preview.endswith("…"))

    def test_short_text_kept(self):
        preview = sc_preview(_record(1, uname="小明", message="你好"), "你好")
        self.assertEqual(preview, "你好")

    def test_long_text_without_hit_is_cut(self):
        preview = sc_preview(_record(1, message="长" * 200), "不存在", limit=20)
        self.assertIn("长" * 20, preview)
        self.assertTrue(preview.endswith("…"))


class IterScHitsTests(unittest.TestCase):
    def _records(self):
        # 最新 → 更旧：index 0 最新
        return [
            _record(0, message="最新的 打招呼"),
            _record(1, message="普通消息"),
            _record(2, message="更早的 打招呼"),
            _record(3, message="无关"),
        ]

    def test_latest_first_and_index(self):
        hits, truncated = iter_sc_hits(self._records(), "打招呼")
        self.assertFalse(truncated)
        self.assertEqual([hit.index for hit in hits], [0, 2])
        self.assertEqual([hit.sc_id for hit in hits], ["1000", "1002"])

    def test_empty_query_does_not_consume_stream(self):
        consumed = []

        def stream():
            for record in self._records():
                consumed.append(record)
                yield record

        hits, truncated = iter_sc_hits(stream(), "  ")
        self.assertEqual((hits, truncated), ([], False))
        self.assertEqual(consumed, [])

    def test_limit_marks_truncated(self):
        records = [_record(i, message="命中") for i in range(5)]
        hits, truncated = iter_sc_hits(records, "命中", limit=3)
        self.assertEqual(len(hits), 3)
        self.assertTrue(truncated)

    def test_limit_exact_has_no_truncation(self):
        records = [_record(i, message="命中") for i in range(3)]
        hits, truncated = iter_sc_hits(records, "命中", limit=3)
        self.assertEqual(len(hits), 3)
        self.assertFalse(truncated)

    def test_hit_from_record(self):
        hit = sc_hit_from_record(_record(7, uname="小明", message="内容"), 7, "内容")
        self.assertIsInstance(hit, ScSearchHit)
        self.assertEqual(hit.index, 7)
        self.assertEqual(hit.sc_id, "1007")
        self.assertEqual(hit.uname, "小明")
        self.assertIn("内容", hit.preview)
        self.assertIn("小明", sc_hit_tooltip(hit))


class DanmakuMatchTests(unittest.TestCase):
    def test_line_match(self):
        self.assertTrue(dm_line_matches("12:00:00 小明: Hello", "hello"))
        self.assertFalse(dm_line_matches("12:00:00 小明: Hello", ""))
        self.assertFalse(dm_line_matches("12:00:00 小明: Hello", "不存在"))

    def test_match_indices_in_document_order(self):
        lines = ["a 命中", "b", "c 命中", "d 命中"]
        self.assertEqual(dm_match_indices(lines, "命中"), [0, 2, 3])
        self.assertEqual(dm_match_indices(lines, ""), [])


class StepAndCounterTests(unittest.TestCase):
    def test_step_wraps(self):
        self.assertEqual(step_index(0, 3, 1), 1)
        self.assertEqual(step_index(2, 3, 1), 0)
        self.assertEqual(step_index(0, 3, -1), 2)
        self.assertEqual(step_index(5, 0, 1), 0)

    def test_counter_text(self):
        self.assertEqual(search_counter(2, 12), "第 3/12 个")
        self.assertEqual(search_counter(0, 0), "无匹配")
        self.assertEqual(search_counter(0, 5, truncated=True), "第 1/5+ 个")
        self.assertEqual(search_counter(99, 5), "第 5/5 个")


if __name__ == "__main__":
    unittest.main()
