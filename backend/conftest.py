"""pytest 引导：将 ``backend`` 目录加入 ``sys.path``，使 ``import shikar`` 可用。"""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
