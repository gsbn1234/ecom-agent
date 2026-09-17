"""结构化落库。

★ 为什么落库和可观测分开：
  可观测（`observability/`）的产品是**文件**，落库的产品是**行**。
  两者的生命周期不同 —— 文件是"这次 run 的现场"，行是"跨 run 的历史"。
  混在一起最容易出的问题是：为了查一条历史记录，得先把 runs 目录翻一遍。

★ 这一层唯一的硬约束是事务边界：一次 run 的头部行与它的全部数据行
  必须在同一个事务里提交。理由写在 `repository.py` 的文件头。
"""
from ecom_agent.store.repository import Repository

__all__ = ["Repository"]
