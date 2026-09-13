#!/usr/bin/env python3
"""Run the isolated, GET-only public research producer."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from readonly_research_producer import main  # noqa: E402

if __name__ == "__main__":
    main()
