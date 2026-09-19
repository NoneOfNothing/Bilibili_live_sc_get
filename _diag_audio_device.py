"""临时诊断脚本：切换系统默认输出设备时，Qt 能否感知、预览能否跟随。

用法：
1. 运行 `python _diag_audio_device.py`（会打开一个预览浮窗，不需要真在播放）；
2. **运行期间**去系统「声音设置」里切换默认输出设备（例如扬声器 ↔ 耳机），多切几次；
3. 看终端每 3 秒打印的一行：
   - 「系统默认」是否跟着变 → 若一直不变，说明 **Qt 没感知到**默认设备变化（得另走 Win32 查询）；
   - 「信号」次数是否增加 → 反映 `audioOutputsChanged` 是否触发；
   - 「预览各路」是否跟着变 → 不变说明切换（重建 QAudioOutput）没生效；
   - 「窗口记录」是窗口内部记住的设备，用于比对。
"""

import queue
import time
from types import SimpleNamespace

from PySide6.QtCore import QTimer
from PySide6.QtMultimedia import QMediaDevices
from PySide6.QtWidgets import QApplication

from blive_sc_get.app_config import load_app_config
from blive_sc_get.qt_preview import PreviewWindow

app = QApplication([])

host = SimpleNamespace(
    app_config=load_app_config(),
    hub=SimpleNamespace(api=None, submit=lambda coro: None),
    ui_queue=queue.Queue(),
    real_room_id=lambda room_id: room_id,
    refresh_preview_button=lambda: None,
)

window = PreviewWindow(host)
window.start(1)          # 随便占一路（不联网也能建好 tile 与音频输出）
window.show()

signals = []
media = QMediaDevices()          # 必须用实例：类属性上的信号连不上
media.audioOutputsChanged.connect(lambda: signals.append(time.monotonic()))

print("RESULT 本机输出设备："
      + "、".join(d.description() for d in QMediaDevices.audioOutputs()), flush=True)
print("RESULT 现在请在系统「声音设置」里切换默认输出设备（多切几次）；"
      "本脚本每 3 秒打印一次状态。", flush=True)


def tick() -> None:
    default = QMediaDevices.defaultAudioOutput()
    tiles = "、".join(tile.audio.device().description() for tile in window._tiles)
    print(f"[{time.strftime('%H:%M:%S')}] "
          f"系统默认=「{default.description()}」 "
          f"窗口记录=「{window._audio_device.description()}」 "
          f"信号={len(signals)} "
          f"预览各路={tiles or '（无）'}",
          flush=True)


timer = QTimer()
timer.timeout.connect(tick)
timer.start(3000)
tick()
app.exec()
