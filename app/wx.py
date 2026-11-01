"""wx 命令入口：python wx.py <命令> …（装好后直接 wx <命令>）。代码在 wxkit/ 里。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from wxkit.cli import main  # noqa: E402

if __name__ == '__main__':
    main()
