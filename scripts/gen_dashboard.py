"""生成可视化面板数据：python scripts/gen_dashboard.py [--days 14]"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dashboard.render import main

if __name__ == "__main__":
    main()
