# photo_album 代码审查报告

> 审查范围：`photo_album/` 源码、`tests/`、入口脚本与 `packaging/`  
> 审查日期：当前会话  
> 整体评价：代码结构清晰、GTK4/libadwaita 使用熟练、测试覆盖核心路径，但存在若干**全局状态污染、资源泄漏风险、错误静默吞没和竞态条件**，需要优先修复。

---

## 一、严重/高优先级问题（建议立即修复）

### 1. 全局 `warnings.filterwarnings` 抑制所有弃用警告
**文件**：
- `photo_album/scanner.py:24`
- `photo_album/viewer.py:22`
- `photo_album/exif_editor.py:22`

**问题**：
- `scanner.py`/`viewer.py` 虽然加了 `module=__name__`，但仍会抑制**本模块所有** `DeprecationWarning`。
- `exif_editor.py` 最危险：`warnings.filterwarnings("ignore", category=DeprecationWarning)` 没有限定模块，会**全局抑制整个进程**的弃用警告，掩盖未来升级 GTK/PyGObject/Pillow 时的重要信号。

**修复建议**：
```python
# 删除模块级的 warnings.filterwarnings
# 如需抑制 GExiv2 的弃用警告，改为在调用点使用 warnings.catch_warnings 上下文：
with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    raw_orientation = metadata.get_tag_string("Exif.Image.Orientation")
```

---

### 2. 多处资源/后台任务在窗口/控件销毁时未清理
**文件**：
- `photo_album/wall.py:132` — `GLib.timeout_add(110, self._check_width)` 永不停止
- `photo_album/wall.py:191-192` — `idle_add`/`timeout_add` 在 `set_items` 中创建，未追踪 ID
- `photo_album/window.py:143` — `GLib.idle_add(self._maximize_idle)` 未保存 source id
- `photo_album/preferences.py:92` — `GLib.idle_add(self._update_cache_row)` 未保存 source id
- `photo_album/viewer.py:269` — `ThreadPoolExecutor` 若 `__init__` 中途异常，可能泄漏

**问题**：
GTK4 控件销毁后，未移除的 GLib source/timeout 仍会回调，轻则日志报错，重则访问已释放的 GObject 导致崩溃。`MediaViewer` 的线程池若构造中途失败，`__init__` 不会走到 `close-request`，线程池无法关闭。

**修复建议**：
- 所有 `timeout_add`/`idle_add` 返回的 source id 保存到实例字段；在 `do_unroot` / `close-request` / `destroy` 回调中 `GLib.source_remove()`。
- `Gallery` 增加 `do_unroot` 覆盖：
  ```python
  def do_unroot(self) -> None:
      if self._width_check_source:
          GLib.source_remove(self._width_check_source)
          self._width_check_source = 0
      self._cancel_all_tasks()
      Gtk.Box.do_unroot(self)
  ```
- `MediaViewer.__init__` 使用 try/finally 或延迟创建线程池，确保异常时关闭。

---

### 3. 时间戳转换未处理无效/极端值
**文件**：
- `photo_album/scanner.py:168-169`
- 调用点：`scanner.py:348`、`scanner.py:413`

**问题**：
```python
def _from_timestamp(timestamp: float) -> datetime:
    return datetime.fromtimestamp(timestamp)
```
`datetime.fromtimestamp` 在传入过大/负数/NaN 时会抛出 `OSError`/`ValueError`/`OverflowError`，导致整张照片被丢弃（`_probe_item` 会异常上抛，外层未捕获）。

**修复建议**：
```python
def _from_timestamp(timestamp: float) -> datetime | None:
    try:
        return datetime.fromtimestamp(timestamp)
    except (OSError, ValueError, OverflowError):
        return None
```
调用处相应处理 `None`（回退到当前时间或只记录错误）。

---

### 4. 扫描线程与 GTK 主线程存在竞态
**文件**：
- `photo_album/window.py:547-577`（`_scan_worker`）
- `photo_album/window.py:612-630`（`_on_scan_batch`）

**问题**：
- `_scan_worker` 闭包直接引用 `self`，扫描完成后的 `GLib.idle_add` 回调可能落在已关闭的窗口上；虽然有 `generation` 与 `_closed` 检查，但 `GLib.idle_add` 入队后、执行前窗口被关闭，仍会触发回调。
- `_on_scan_batch` 中 `self._all_items = list(existing.values())` 与主线程 UI 操作共享可变列表，极端情况下读写并发。

**修复建议**：
- 扫描线程只通过线程安全队列/idle 回调与主线程通信；回调入口统一检查 `_closed` 并立即返回。
- `_on_scan_batch` 收到批次后先合并到本地 dict，再一次性替换 `self._all_items`，避免中间状态被读取。

---

### 5. `Config.save()` / `ThumbnailCache.write()` 等重要 IO 静默失败
**文件**：
- `photo_album/config.py:183-197`
- `photo_album/thumbnails.py:83-93`

**问题**：
```python
except OSError:
    pass
```
配置保存失败、缓存写入失败时没有任何日志或反馈，用户可能误以为设置已生效。

**修复建议**：
- 至少通过 `GLib.warning` / `logging.warning` 输出错误。
- `Config.save()` 可考虑返回 `bool` 让调用方在 UI 上提示用户。

---

### 6. 缩略图缓存键依赖 `mtime` 但 HEIC/视频编辑后 `mtime` 可能不变
**文件**：
- `photo_album/thumbnails.py:58-69`

**问题**：
缓存键使用 `mtime + size + version`。如果用户编辑 EXIF 日期或旋转图片但文件大小与修改时间未变（例如某些编辑器原地修改），旧缩略图不会失效。

**修复建议**：
若可廉价获取，将文件 inode 变更序列号（`st_ctime_ns`）或内容哈希前若干字节纳入 key；或在 `window._on_exif_saved` 中已手动删除缓存，确保此行为覆盖所有编辑场景。

---

### 7. `MediaViewer` 照片加载任务不检查 `_closed`
**文件**：
- `photo_album/viewer.py:489-504`
- `photo_album/viewer.py:506-512`

**问题**：
`_show_photo` 立即向线程池提交 `_load_full_image`；即使窗口已关闭，线程仍会解码大图，浪费 CPU/内存，回调在 `idle_add` 中还会访问对象。

**修复建议**：
```python
def _show_photo(self, item: MediaItem) -> None:
    if self._closed:
        return
    ...
    self._executor.submit(self._load_full_image, item, generation)

def _load_full_image(self, item: MediaItem, generation: int) -> None:
    if self._closed:
        return
    ...
```

---

### 8. `_probe_ffprobe` 与 JSON 解析异常处理不精确
**文件**：
- `photo_album/scanner.py:172-198`

**问题**：
```python
except ValueError:
    return None
```
`json.loads` 抛出的应是 `json.JSONDecodeError`（`ValueError` 子类），此处捕获过宽。同时 `proc.stdout` 可能极大，对恶意/损坏文件可能造成内存尖峰。

**修复建议**：
```python
try:
    return json.loads(proc.stdout)
except json.JSONDecodeError:
    return None
```
可考虑限制 stdout 大小（`stdout=subprocess.PIPE` 前无法限制，可在调用处限制文件）。

---

### 9. `_video_metadata` 中 `side_data_list` 可能非可迭代对象
**文件**：
- `photo_album/scanner.py:318-324`

**问题**：
```python
for side in stream.get("side_data_list", []) or []:
```
若 ffprobe 返回 `"side_data_list": None`，`or []` 可兜底；但若返回字符串或其他非可迭代对象，仍会 `TypeError`。

**修复建议**：
```python
side_data_list = stream.get("side_data_list") or []
if isinstance(side_data_list, list):
    for side in side_data_list:
        ...
```

---

## 二、中优先级问题（建议修复）

### 10. 版本号不一致
**文件**：
- `photo_album/__init__.py:12` → `"1.0.0"`
- `photo_album/config.py:17` → `"1.1.2"`
- `pyproject.toml:7` → `"1.1.2"`

**问题**：
包内 `__version__` 与真实版本不一致，会导致 `packaging/build_deb.py`（从 `config.py` 解析）与 pip 元数据不一致。

**修复建议**：
单一事实来源。例如在 `__init__.py` 中：
```python
from importlib.metadata import version
__version__ = version("photo-album")
```
或统一硬编码为 `"1.1.2"`。

---

### 11. `Gdk.Texture.new_from_bytes(GLib.Bytes.new(b""))` 在 Pillow 缺失时可能崩溃
**文件**：
- `photo_album/theme.py:65`、`90`、`102`

**问题**：
返回空 bytes 给 `Gdk.Texture.new_from_bytes` 会抛出 `GLib.Error`，且调用方未处理。

**修复建议**：
生成一个 1×1 透明 PNG 或纯色纹理作为兜底，或返回 `None` 并在绘制时跳过。

---

### 12. `copy_files` / `move_to_trash` / `delete_permanently` 失败无反馈
**文件**：
- `photo_album/file_actions.py`

**问题**：
`copy_files` 返回 `bool`，但 `window._context_copy_files` 已处理提示；`move_to_trash` 与 `delete_permanently` 若 `trash_async`/`delete_async` 自身未抛出但异步失败，回调会返回错误。整体 OK，但 `copy_files` 内部所有 provider 创建失败时直接返回 `False`，不说明原因。

**修复建议**：
增加日志记录具体失败类型，便于排查剪贴板兼容性问题。

---

### 13. `_WidthHost` 自定义 LayoutManager 类型注解缺失
**文件**：
- `photo_album/window.py:35-65`

**问题**：
`do_measure` / `do_allocate` 缺少返回类型注解与参数类型，影响类型检查与可读性。

**修复建议**：
```python
def do_measure(
    self, widget: Gtk.Widget, orientation: Gtk.Orientation, for_size: int
) -> tuple[int, int, int, int]:
    ...

def do_allocate(self, widget: Gtk.Widget, width: int, height: int, baseline: int) -> None:
    ...
```

---

### 14. `MediaViewer` 直接访问 `_items` 私有属性
**文件**：
- `photo_album/window.py:1280`

**问题**：
```python
if any(entry.path == item.path for entry in self._viewer._items):
```
跨类访问私有属性破坏封装。

**修复建议**：
在 `MediaViewer` 暴露公共方法：
```python
def contains(self, item: MediaItem) -> bool:
    return any(entry.path == item.path for entry in self._items)
```

---

### 15. `preferences.py` 的 `_update_cache_row` 可能空指针
**文件**：
- `photo_album/preferences.py:225-230`

**问题**：
```python
if self._cache_row.get_subtitle().startswith("已清空"):
```
虽然 Adw.ActionRow 默认返回空字符串，但文档未保证一定非 `None`。

**修复建议**：
```python
subtitle = self._cache_row.get_subtitle() or ""
if subtitle.startswith("已清空"):
    return False
```

---

### 16. `window.py` 大量 lambda 闭包引用 `self` 未弱引用
**文件**：
- `photo_album/window.py` 多处 `lambda *_: self.xxx()`

**问题**：
GTK 信号连接 lambda 强引用 `self`，理论上信号在对象销毁时会断开；但若外部长期持有对象引用，可能延迟回收。当前不是泄漏主因，但建议对长时间存在的动作/菜单按钮使用带 `@weakref.finalize` 模式或普通方法连接。

**修复建议**：
低优先级；可作为重构项处理。

---

## 三、低优先级/代码风格问题

### 17. 异常捕获过于宽泛
**文件**：
- `photo_album/scanner.py:234-235`、`287-292`、`331-333`
- `photo_album/viewer.py:167-169`、`189-190`、`509-511` 等

**问题**：
大量 `except Exception: pass` 隐藏真实错误，调试困难。

**修复建议**：
- 对预期异常（`GLib.Error`、`OSError`、`ValueError`）做精确捕获。
- 对真正兜底的位置使用 `logging.exception` 记录，再向用户展示友好提示。

---

### 18. `_HEIF_SUFFIXES` 重复定义
**文件**：
- `photo_album/scanner.py:64`
- `photo_album/imaging.py:34`

**修复建议**：
统一放到 `models.py` 的常量中。

---

### 19. `video_frame` 函数签名中的 `duration` 未校验
**文件**：
- `photo_album/imaging.py:229-239`

**问题**：
`duration` 可能为 `NaN` 或负数，导致 `min(1.0, duration / 3)` 异常或错误 seek。

**修复建议**：
```python
duration = max(0.0, float(duration) if not math.isnan(duration) else 0.0)
```

---

### 20. 测试覆盖不足
**文件**：
- `tests/test_core.py`

**问题**：
- 未覆盖：右键删除、回收站失败回退、窗口关闭资源清理、缩略图服务并发取消、Viewer 键盘导航、Timeline 点击跳转。
- 未安装 lint 工具（`flake8`/`pylint`/`ruff`/`mypy`），无法保证静态质量。

**修复建议**：
- 在 `pyproject.toml` 增加可选 dev 依赖：`ruff`、`mypy`、`pytest-cov`。
- 补充 UI 集成测试可使用 `pytest` + 头less 显示环境（`xvfb-run`）。

---

## 四、推荐修复顺序

| 优先级 | 问题 | 影响 |
|--------|------|------|
| P0 | 全局 `warnings.filterwarnings` | 隐藏关键弃用警告，难以维护 |
| P0 | GLib source/timeout 未清理 | 崩溃/内存泄漏 |
| P0 | `_from_timestamp` 未处理异常 | 扫描崩溃 |
| P1 | 扫描线程竞态 | 关闭窗口后崩溃/异常 |
| P1 | 重要 IO 静默失败 | 用户体验差、数据丢失错觉 |
| P1 | 缓存键对编辑后文件不敏感 | 编辑后仍显示旧缩略图 |
| P1 | `MediaViewer` 关闭后仍解码 | CPU/内存浪费 |
| P2 | 版本号不一致 | 发布/打包错误 |
| P2 | 过度宽泛的 `except Exception` | 调试困难 |
| P2 | 测试/lint 工具缺失 | 质量回归 |

---

## 五、可立即落地的工具链建议

1. **安装 lint 与类型检查**：
   ```bash
   python3 -m pip install ruff mypy pytest-cov
   ```
2. **运行检查**：
   ```bash
   ruff check photo_album tests
   mypy photo_album
   python3 -m pytest tests/ --cov=photo_album --cov-report=term-missing
   ```
3. **CI 建议**：在 GitHub Actions/GitLab CI 中加入上述命令，阻止问题回归。

---

*报告结束。如需我直接按此清单提交代码修复，请告诉我优先修复 P0/P1 还是全部处理。*
