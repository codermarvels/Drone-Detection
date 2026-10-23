"""Radio inputs: the real HackRF, a replay of recorded files, or a simulator.

Every source yields max-held sweep frames and can be asked for one IQ
capture. The pipeline does not know which kind it has, so tests run the
same code as the real hardware.
"""

from .base import Capture, Source, SweepFrame

__all__ = ["Capture", "Source", "SweepFrame"]
