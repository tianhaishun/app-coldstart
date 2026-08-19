"""测试环境隔离：把项目持久化目录重定向到临时目录。

真机使用过的仓库里 projects/_device_templates 存有真实设备模板，
check_auto 的模板回退加载（_load_fallback_marker）会扫描该目录，
导致依赖磁盘状态的测试结果漂移（开发者本机红、CI 干净环境绿）。
必须在测试模块 import server 之前设置 CST_PROJECTS_DIR（server.py 在
import 时读取该环境变量），因此放在 conftest 模块级执行——pytest 收集
阶段会先于测试模块导入本文件。
"""
import atexit
import os
import shutil
import tempfile

_TMP_PROJECTS = tempfile.mkdtemp(prefix="cst-test-projects-")
os.environ["CST_PROJECTS_DIR"] = _TMP_PROJECTS
atexit.register(shutil.rmtree, _TMP_PROJECTS, ignore_errors=True)
