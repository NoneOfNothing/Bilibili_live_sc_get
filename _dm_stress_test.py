"""弹幕写入压力测试（独立小程序，不依赖项目代码）——用于人工定位卡死临界点。

用法：调好参数后点「开始」；程序持续往文本区写入弹幕，状态行实时显示
「已写入条数 / 当前行数 / 上一批耗时」。**界面一旦卡死，看状态行最后的
数字**，那就是临界点附近的写入量。可切换各开关，定位是哪个因素导致卡死：

- 行数上限：Text 保留的最大行数，0 = 不裁剪（复现无上限增长）
- 挂标签：是否按真实弹幕挂 dm:<id> / dmbody:<id> / dmuid:<uid> / dme:<unique> 标签
- 图片比例 N：每 N 条弹幕内联一张表情图（162x60 真实 PNG），0 = 全部纯文本
- 图片上限：内嵌图片最多保留几张，超出后**最旧的**回退为文本（0 = 不限制，
  复现旧版全量内联），与正式程序的修复一致
- 跟随滚动：写入后是否 see("end")（模拟自动跟随）

写入逻辑与正式程序一致：先插「[触发词]」占位文字，再就地替换为内联图片。
"""
import time
import tkinter as tk
from collections import deque
from tkinter import ttk

INTERVAL_DEFAULT = 100
BATCH_DEFAULT = 10


class StressApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        root.title("弹幕写入压力测试（卡死定位）")
        root.geometry("820x640")

        bar = ttk.Frame(root)
        bar.pack(side="top", fill="x", padx=6, pady=4)
        self.batch_var = tk.IntVar(value=BATCH_DEFAULT)
        self.interval_var = tk.IntVar(value=INTERVAL_DEFAULT)
        self.cap_var = tk.IntVar(value=4000)
        self.tags_var = tk.BooleanVar(value=True)
        self.ratio_var = tk.IntVar(value=2)
        self.piccap_var = tk.IntVar(value=100)
        self.follow_var = tk.BooleanVar(value=True)
        self.image_order = deque()  # 内嵌图出现顺序（FIFO），见图片上限
        self.running = False
        self.count = 0

        def spin(text, var, lo, hi):
            ttk.Label(bar, text=text).pack(side="left")
            ttk.Spinbox(bar, from_=lo, to=hi, width=7, textvariable=var).pack(
                side="left", padx=(2, 8))

        spin("每批条数", self.batch_var, 1, 200)
        spin("批间隔ms", self.interval_var, 10, 2000)
        spin("行数上限(0=不裁剪)", self.cap_var, 0, 100000)
        spin("图片比例(每N条1图,0=无图)", self.ratio_var, 0, 50)
        spin("图片上限(0=不限)", self.piccap_var, 0, 10000)
        ttk.Checkbutton(bar, text="挂标签", variable=self.tags_var).pack(side="left")
        ttk.Checkbutton(bar, text="跟随滚动", variable=self.follow_var).pack(side="left")

        self.start_btn = ttk.Button(bar, text="开始", command=self.start)
        self.start_btn.pack(side="left", padx=(8, 2))
        ttk.Button(bar, text="重置", command=self.reset).pack(side="left", padx=2)

        self.status_var = tk.StringVar(value="待开始")
        ttk.Label(root, textvariable=self.status_var, foreground="#1a5fb4",
                  font=("Microsoft YaHei UI", 10, "bold")).pack(
            side="top", fill="x", padx=6)

        frame = ttk.Frame(root)
        frame.pack(side="top", fill="both", expand=True)
        self.text = tk.Text(frame, wrap="word", font=("Microsoft YaHei UI", 10),
                            padx=6, pady=4)
        scroll = ttk.Scrollbar(frame, command=self.text.yview)
        self.text.configure(yscrollcommand=scroll.set, state="disabled")
        scroll.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)

        # 预生成 8 张不同颜色/尺寸的表情图，模拟多个不同表情
        self.images = []
        colors = ["#e01b24", "#3584e4", "#2ec27e", "#f6d32d", "#c061cb",
                  "#ff7800", "#986a44", "#000000"]
        for i, color in enumerate(colors):
            img = tk.PhotoImage(width=162, height=60)
            img.put(color, to=(2, 2, 160, 58))
            self.images.append(img)

    # ---------- 写入（与正式程序同款逻辑） ----------

    @staticmethod
    def _int(var, default: int) -> int:
        """读参数框里的数字：手输非法/清空时回退默认，不让写入中断。"""
        try:
            return int(var.get())
        except (tk.TclError, TypeError, ValueError):
            return default

    def write_batch(self) -> None:
        text = self.text
        batch = self._int(self.batch_var, 10)
        use_tags = bool(self.tags_var.get())
        ratio = self._int(self.ratio_var, 0)
        follow = bool(self.follow_var.get())
        cap = self._int(self.cap_var, 4000)
        text.configure(state="normal")
        for k in range(batch):
            self.count += 1
            i = self.count
            dmid = str(10**17 + i)
            dm_tag = f"dm:{dmid}" if use_tags else ""
            body_tag = f"dmbody:{dmid}" if use_tags else ""
            user_tag = f"dm_user dmuid:{i % 50}" if use_tags else ""
            emote_tag = f"dme:room_9527_{i % 8}" if use_tags else ""
            text.insert("end", f"[20:15:{i % 60:02d}] ", f"dm_time {dm_tag}".strip())
            text.insert("end", f"弹幕哥{i % 50}：", f"{user_tag} {dm_tag}".strip())
            has_image = ratio > 0 and i % ratio == 0
            if has_image:
                # 与正式程序一致：先插占位文本，再就地替换为内联图片
                text.insert("end", "[表情]", f"{dm_tag} {body_tag} {emote_tag}".strip())
                text.delete(f"end-6c", "end-1c")
                text.image_create("end-1c", image=self.images[i % 8], padx=1, pady=0)
                self.image_order.append(emote_tag)
            else:
                text.insert("end", f"第 {i} 条弹幕内容测试文字测试文字",
                            f"{dm_tag} {body_tag}".strip())
            text.insert("end", "\n", dm_tag or ())
        # 图片上限：超出后把最旧的内嵌图回退为文本
        pic_cap = self._int(self.piccap_var, 100)
        overflow = len(self.image_order) - pic_cap if pic_cap > 0 else 0
        for _ in range(max(0, overflow)):
            tag = self.image_order.popleft()
            ranges = text.tag_ranges(tag)
            if len(ranges) < 2:
                continue
            start, end = ranges[0], ranges[1]
            if text.compare(end, "==", f"{start}+1c"):  # 1 字符位 = 图片
                text.delete(start, end)
                text.insert(start, "[表情]", tuple(
                    {str(n) for n in text.tag_names(start)} | {tag}))
        if cap > 0:
            lines = int(text.index("end-1c").split(".")[0])
            if lines > cap:
                text.delete("1.0", f"{lines - cap // 2}.0")
        text.configure(state="disabled")
        if follow:
            text.see("end")

    def tick(self) -> None:
        if not self.running:
            return
        t0 = time.perf_counter()
        self.write_batch()
        cost = (time.perf_counter() - t0) * 1000
        lines = int(self.text.index("end-1c").split(".")[0])
        self.status_var.set(
            f"已写入 {self.count} 条 | 行数 {lines} | 内嵌图 {len(self.image_order)} | "
            f"上一批 {cost:.0f}ms | 累计 {time.perf_counter() - self.t0:.0f}s")
        if cost > 300:
            self.status_var.set(self.status_var.get() + "  ⚠ 本批明显变慢")
        self.root.after(max(10, self._int(self.interval_var, INTERVAL_DEFAULT)), self.tick)

    def start(self) -> None:
        self.running = not self.running
        self.start_btn.configure(text="暂停" if self.running else "开始")
        if self.running:
            self.t0 = time.perf_counter()
            self.tick()

    def reset(self) -> None:
        self.running = False
        self.start_btn.configure(text="开始")
        self.count = 0
        self.image_order.clear()
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")
        self.status_var.set("已重置，待开始")


if __name__ == "__main__":
    root = tk.Tk()
    StressApp(root)
    root.mainloop()
