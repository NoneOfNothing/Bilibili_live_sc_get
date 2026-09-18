"""直播预览（Qt 版 P1）的离线测试：清晰度档位 / 文案 / 自动档选择（不联网、不起窗口）。"""

import unittest

from blive_sc_get.app_config import PREVIEW_QUALITY_CHOICES
from blive_sc_get.qt_preview import (
    ANONYMOUS_MAX_QUALITY,
    AUTO_QUALITY,
    HEALTH_GRACE_S,
    HEALTH_MAX_AUTO_RELOADS,
    HEALTH_MAX_RELOADS_SUB,
    MAX_QUALITY,
    QUALITY_TEXTS,
    DRIFT_MIN_INTERVAL_S,
    DRIFT_SYNC_GRACE_S,
    available_qualities,
    describe_preview_error,
    format_duration,
    grid_shape,
    needs_auto_reload,
    needs_catch_up,
    needs_watch_report,
    pick_quality,
    pick_victim_index,
    process_cpu_ratio,
    quality_text,
)


class PreviewQualityTests(unittest.TestCase):
    """清晰度档位：**未登录只能到 720P**（B 站对匿名请求的限制）。"""

    def test_anonymous_limited_to_720p(self):
        allowed = available_qualities(False)
        self.assertIn(ANONYMOUS_MAX_QUALITY, allowed)
        self.assertTrue(all(qn <= ANONYMOUS_MAX_QUALITY for qn in allowed),
                        f"未登录时不应给出更高档位：{allowed}")
        self.assertNotIn(400, allowed)   # 蓝光及以上需要登录

    def test_logged_in_offers_all_real_choices(self):
        """兜底列表只含真实档位（「自动」由界面单独加，不是可请求的 qn）。"""
        expected = tuple(qn for qn in PREVIEW_QUALITY_CHOICES if qn > AUTO_QUALITY)
        self.assertEqual(available_qualities(True), expected)
        self.assertNotIn(AUTO_QUALITY, expected)

    def test_every_choice_has_text(self):
        """每个可选档位都要有中文名，否则下拉里会出现「qn=xxx」。"""
        for qn in PREVIEW_QUALITY_CHOICES:
            self.assertIn(qn, QUALITY_TEXTS)
            self.assertFalse(quality_text(qn).startswith("qn="),
                             f"{qn} 缺少中文名")
        self.assertEqual(quality_text(AUTO_QUALITY), "自动（最高）")

    def test_unknown_quality_falls_back(self):
        self.assertEqual(quality_text(999), "qn=999")
        self.assertEqual(quality_text("80"), "流畅")   # 字符串也接受
        self.assertEqual(quality_text(None), "None")


class PickQualityTests(unittest.TestCase):
    """``pick_quality``：默认自动取该房间可用最高档，指定档位不可用时回退。"""

    def test_auto_picks_highest_available(self):
        self.assertEqual(pick_quality(AUTO_QUALITY, (10000, 400, 250)), 10000)
        self.assertEqual(pick_quality(AUTO_QUALITY, (250, 400)), 400)  # 顺序无关

    def test_auto_without_accept_requests_max(self):
        """接口没给可用档位时，用上限请求、由服务端按权限降级。"""
        self.assertEqual(pick_quality(AUTO_QUALITY, ()), MAX_QUALITY)

    def test_explicit_choice_kept_when_available(self):
        self.assertEqual(pick_quality(250, (10000, 400, 250)), 250)

    def test_explicit_choice_falls_back_when_missing(self):
        """选了该房间没有的档位（例如未登录想选 4K）→ 回退到可用最高。"""
        self.assertEqual(pick_quality(20000, (10000, 400, 250)), 10000)
        self.assertEqual(pick_quality(20000, (150, 80)), 150)

    def test_explicit_choice_without_accept_kept(self):
        self.assertEqual(pick_quality(400, ()), 400)

    def test_bad_values_are_tolerated(self):
        self.assertEqual(pick_quality("150", (250, 150, 80)), 150)
        self.assertEqual(pick_quality(None, (250, 150)), 250)
        self.assertEqual(pick_quality(150, (-1, 0, "x", 150)), 150)


class NeedsAutoReloadTests(unittest.TestCase):
    """断流自愈的判定（纯函数）：**在播 + 未暂停 + 窗口可见 + 播放器停住** → 自动重连。"""

    @staticmethod
    def _call(**overrides):
        base = dict(active=True, paused=False, visible=True, playing=False,
                    live=True, since_load=HEALTH_GRACE_S + 1.0, auto_reloads=0)
        base.update(overrides)
        return needs_auto_reload(**base)

    def test_stalled_playback_triggers_reload(self):
        self.assertTrue(self._call())

    def test_playing_is_healthy(self):
        self.assertFalse(self._call(playing=True))

    def test_user_pause_is_not_a_stall(self):
        """用户主动暂停（画面定格）不能被当成断流，否则一暂停就自动重连。"""
        self.assertFalse(self._call(paused=True))

    def test_offline_room_is_not_retried(self):
        """主播未开播：流本来就不存在，重试没意义。"""
        self.assertFalse(self._call(live=False))

    def test_stopped_or_hidden_preview_skipped(self):
        self.assertFalse(self._call(active=False))
        self.assertFalse(self._call(visible=False))

    def test_grace_period_after_load(self):
        """刚装载完（起播 / 缓冲）不判断流——否则误重载会让它永远起不来。"""
        self.assertFalse(self._call(since_load=0.0))
        self.assertFalse(self._call(since_load=HEALTH_GRACE_S - 0.1))
        self.assertTrue(self._call(since_load=HEALTH_GRACE_S))

    def test_retry_budget_is_limited(self):
        self.assertTrue(self._call(auto_reloads=HEALTH_MAX_AUTO_RELOADS - 1))
        self.assertFalse(self._call(auto_reloads=HEALTH_MAX_AUTO_RELOADS))
        self.assertFalse(self._call(auto_reloads=HEALTH_MAX_AUTO_RELOADS + 5))

    def test_sub_stream_has_smaller_budget(self):
        """副路只重连 ``HEALTH_MAX_RELOADS_SUB`` 次（多路时不为了看不到的画面拖垮整机）。"""
        self.assertLess(HEALTH_MAX_RELOADS_SUB, HEALTH_MAX_AUTO_RELOADS)
        self.assertTrue(self._call(auto_reloads=0, max_reloads=HEALTH_MAX_RELOADS_SUB))
        self.assertFalse(self._call(auto_reloads=HEALTH_MAX_RELOADS_SUB,
                                    max_reloads=HEALTH_MAX_RELOADS_SUB))


class NeedsCatchUpTests(unittest.TestCase):
    """自动追边（播放落后）：播放中累计落后超阈值才追，且有宽限期与最小间隔。"""

    @staticmethod
    def _call(**overrides):
        base = dict(playing=True, paused=False, drift_s=3.5, limit_s=3.0,
                    since_sync_s=600.0, since_catch_up_s=600.0)
        base.update(overrides)
        return needs_catch_up(**base)

    def test_triggers_when_drift_exceeds_limit(self):
        self.assertTrue(self._call())

    def test_below_limit_or_disabled(self):
        self.assertFalse(self._call(drift_s=2.9))
        self.assertFalse(self._call(limit_s=0.0))   # 0 = 用户关闭自动追边

    def test_not_while_paused_or_stalled(self):
        self.assertFalse(self._call(paused=True))
        self.assertFalse(self._call(playing=False))

    def test_grace_after_load_and_min_interval(self):
        self.assertFalse(self._call(since_sync_s=DRIFT_SYNC_GRACE_S - 1))
        self.assertFalse(self._call(since_catch_up_s=DRIFT_MIN_INTERVAL_S - 1))
        self.assertTrue(self._call(since_sync_s=DRIFT_SYNC_GRACE_S))
        self.assertTrue(self._call(since_catch_up_s=DRIFT_MIN_INTERVAL_S))


class GridShapeTests(unittest.TestCase):
    """宫格行列数（多路布局）：1 路全屏、2 路左右分栏、3~4 路 2×2。"""

    def test_shapes(self):
        self.assertEqual(grid_shape(0), (0, 0))
        self.assertEqual(grid_shape(1), (1, 1))
        self.assertEqual(grid_shape(2), (1, 2))
        self.assertEqual(grid_shape(3), (2, 2))   # 3 路也留一格空位，比 1×3 均称
        self.assertEqual(grid_shape(4), (2, 2))

    def test_large_counts_fall_back_to_near_square(self):
        rows, columns = grid_shape(9)
        self.assertEqual(rows * columns >= 9, True)
        self.assertEqual((rows, columns), (3, 3))


class PickVictimTests(unittest.TestCase):
    """资源保护：CPU 偏高时停**最后加入的副路**；只有一路时不动手。"""

    def test_last_sub_is_dropped_first(self):
        self.assertEqual(pick_victim_index(4, 0), 3)   # 主路是第 0 路 → 停第 4 路
        self.assertEqual(pick_victim_index(3, 2), 1)   # 主路在末尾 → 停中间那路
        self.assertEqual(pick_victim_index(2, 1), 0)

    def test_single_room_is_kept(self):
        self.assertIsNone(pick_victim_index(1, 0))
        self.assertIsNone(pick_victim_index(0, 0))


class DescribePreviewErrorTests(unittest.TestCase):
    """拉流失败文案（P4）：把错误码 / 加密状态翻成一句可操作的中文。"""

    def test_encrypted_room_asks_for_password(self):
        text = describe_preview_error({}, encrypted=True, pwd_verified=False)
        self.assertIn("加密", text)
        self.assertIn("输入密码", text)

    def test_wrong_password_hint(self):
        text = describe_preview_error({}, encrypted=True, pwd_verified=False, pwd_used=True)
        self.assertIn("密码未通过", text)

    def test_encrypted_and_verified_is_not_a_password_problem(self):
        """加密但已通过密码时不应提示输密码（该情况要按具体错误说明）。"""
        text = describe_preview_error({"flv": "网络请求失败"}, encrypted=True,
                                      pwd_verified=True)
        self.assertNotIn("密码", text)

    def test_known_codes_are_translated(self):
        self.assertIn("未开播", describe_preview_error({"flv": "code=-400 房间未开播"}))
        self.assertIn("风控", describe_preview_error({"flv": "code=-352 风控"}))
        self.assertIn("未登录", describe_preview_error({"flv": "code=-101 未登录"}))
        self.assertIn("网络", describe_preview_error({"flv": "aiohttp ClientError boom"}))

    def test_empty_errors_fallback(self):
        self.assertIn("未返回可播放地址", describe_preview_error({}))
        self.assertIn("未返回可播放地址", describe_preview_error(None))

    def test_unknown_reason_passed_through(self):
        self.assertEqual(describe_preview_error({"flv": "奇怪的原因"}), "flv: 奇怪的原因")


class ProcessCpuRatioTests(unittest.TestCase):
    """CPU 占用率换算（纯函数）。"""

    def test_ratio(self):
        self.assertAlmostEqual(process_cpu_ratio(0.5, 1.0), 0.5)
        self.assertAlmostEqual(process_cpu_ratio(2.0, 1.0), 2.0)  # 多核可超过 1
        self.assertEqual(process_cpu_ratio(1.0, 0.0), 0.0)        # 墙上时间为 0 时不炸
        self.assertEqual(process_cpu_ratio(-1.0, 1.0), 0.0)


class NeedsWatchReportTests(unittest.TestCase):
    """观看时长上报的判定：只有「真正在看」才上报（ROADMAP 63 · P2）。"""

    @staticmethod
    def _call(**overrides):
        base = dict(enabled=True, active=True, paused=False, live=True, playing=True)
        base.update(overrides)
        return needs_watch_report(**base)

    def test_reports_while_playing_live(self):
        self.assertTrue(self._call())

    def test_disabled_switch_or_inactive_preview_skipped(self):
        self.assertFalse(self._call(enabled=False))   # 开关没开（默认）
        self.assertFalse(self._call(active=False))    # 没在预览

    def test_paused_is_not_counted(self):
        """暂停时画面定格，不该算观看时长。"""
        self.assertFalse(self._call(paused=True))

    def test_offline_room_is_not_counted(self):
        self.assertFalse(self._call(live=False))

    def test_stalled_player_is_not_counted(self):
        """断流 / 缓冲（播放器没在播放）期间不计。"""
        self.assertFalse(self._call(playing=False))


class FormatDurationTests(unittest.TestCase):
    """已上报时长的显示格式。"""

    def test_minutes_and_hours(self):
        self.assertEqual(format_duration(0), "00:00")
        self.assertEqual(format_duration(59), "00:59")
        self.assertEqual(format_duration(60), "01:00")
        self.assertEqual(format_duration(3599), "59:59")
        self.assertEqual(format_duration(3661), "1:01:01")

    def test_negative_treated_as_zero(self):
        self.assertEqual(format_duration(-5), "00:00")


if __name__ == "__main__":
    unittest.main()
