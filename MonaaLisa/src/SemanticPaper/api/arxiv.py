import arxiv as arx
from util.logger import Logger

"""
Original File by Basti - 04-May-2025
Refactored into a class by Lenio - 20-September-2025
Reduced to the client used by the incremental pipeline - Nov 2026
Abstract: Holds the configured arxiv.Client used to query the arXiv API.
"""
class ArxivAPI:

    def __init__(self):
        self.logger = Logger("ArxivAPI")
        # page_size 500 statt 100: ~5x weniger Requests beim monatlichen Abgleich
        # (~25k Paper). delay_seconds=3 entspricht der arXiv-Nutzungsrichtlinie,
        # num_retries=5 fängt die häufigen leeren Seiten / Timeouts der API ab.
        self.client = arx.Client(page_size=500, delay_seconds=3.0, num_retries=5)
