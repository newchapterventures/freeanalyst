"""测试套的全局隔离：**不许碰用户的真实文件**。

这里只做一件事，但它是踩出来的：

`webapp.note_appraise()` 会在"成功出一次估值"时给人真实的使用状态记一笔
（`~/.freeanalyst/ui-state.json`）。而测试里有好几处会真的跑 `api_appraise` ——
于是**跑一次测试，用户的 runs 就从 0 变成 10**，引导从此再也不弹。

所以测试一开始就把路径指到临时目录。放在包的 `__init__.py` 里，是因为
`unittest discover` 会先导入这个包，任何测试模块都跑在它之后。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

#: 只在不被外部显式指定的情况下接管（让人还能手动指到别处调试）
os.environ.setdefault(
    "FREANALYST_UI_STATE",
    str(Path(tempfile.mkdtemp(prefix="freeanalyst-test-")) / "ui-state.json"),
)
