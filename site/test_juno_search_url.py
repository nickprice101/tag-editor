"""
Test that the Juno search URL uses the correct q%5Btitle%5D%5B0%5D format.
"""

import re
import os

APP_PY = os.path.join(os.path.dirname(__file__), "app.py")


def test_juno_search_url_format():
    """Juno search URL must use solrorder=relevancy&q%5Btitle%5D%5B0%5D= format."""
    with open(APP_PY) as f:
        source = f.read()
    pattern = r'https://www\.junodownload\.com/search/\?solrorder=relevancy&q%5Btitle%5D%5B0%5D='
    assert re.search(pattern, source), (
        "Juno search URL should use "
        "?solrorder=relevancy&q%5Btitle%5D%5B0%5D= format"
    )


def test_juno_old_url_format_removed():
    """Old Juno search URL using q[keywords] must not be present."""
    with open(APP_PY) as f:
        source = f.read()
    assert r'q[keywords]' not in source, (
        "Old Juno URL format q[keywords] should have been replaced"
    )
