"""pytest 全局夹具。

★ 这里只做一件事：把项目根插进 sys.path。
  pytest 默认只把「用例所在目录」加进 sys.path（rootdir 模式），不认 ecom_agent 这个包。
  没有这行，`from ecom_agent.dsl.loader import load` 会 ModuleNotFoundError，
  而且报错信息会误导你去查包结构，而不是查 sys.path —— 这个坑我踩过。

  注：装成 editable（pip install -e .）之后其实不需要这行，但那样测试就依赖
  "环境装对了"这个前提，clone 下来直接 pytest 会失败。这行的成本是两行，
  收益是测试不依赖安装状态。
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
