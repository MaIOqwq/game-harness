# -*- coding: utf-8 -*-
"""入口: python -m harness.runner [--db path] [--live]"""
import os
import sys

# 允许从任意 cwd 以 python -m 运行(把项目根放到 path 首位)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from harness.runner.shell import main

sys.exit(main())
