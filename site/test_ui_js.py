"""
Regression test: verify the inline JavaScript in ui_home() has no syntax errors.

The f-string in ui_home() contains JavaScript embedded in Python.
In Python f-strings, \' is processed as ' (backslash consumed), which can
break JS single-quoted strings. The fix is to use \\' in Python source to
produce \' in the generated JavaScript.

This test:
  1. Extracts the <script> block from the ui_home() HTML template.
  2. Applies minimal f-string processing ({{ -> {, }} -> }).
  3. Validates the resulting JS with Node.js --check.
"""

import os
import re
import subprocess
import sys
import tempfile

APP_PY = os.path.join(os.path.dirname(__file__), "app.py")


def extract_script_block(source: str) -> str:
    """Extract the first <script>...</script> block from the f-string template."""
    m = re.search(r"<script>\n(.*?)\n</script>", source, re.DOTALL)
    assert m, "<script> block not found in ui_home() template"
    return m.group(1)


def minimal_fstring_to_js(raw: str) -> str:
    """
    Convert the Python f-string literal portions to plain text suitable for
    JS syntax checking.

    Steps:
    1. {{ -> {, }} -> } (undo Python f-string brace escaping)
    2. Substitute dummy values for the two Python variables used in the script.
    3. Process Python backslash escape sequences (e.g. \\\\ -> \\, so that the
       generated JS text matches what Python would actually produce at runtime).
    """
    js = raw.replace("{{", "{").replace("}}", "}")
    # Replace Python variable references with dummy strings
    js = js.replace("{browse_default}", '"/mnt/music"')
    js = js.replace("{path}", '""')
    # Process Python \\ -> \ escape sequences at the bytes level.
    # In the source file, \\' (two backslashes + quote) represents a Python
    # string escape that produces \' (one backslash + quote) at runtime.
    js = js.encode("utf-8").replace(b"\\\\", b"\\").decode("utf-8")
    return js


def validate_js_syntax(js: str) -> None:
    """Run Node.js --check on a temporary file containing the JS."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".js", delete=False
    ) as tmp:
        tmp.write(js)
        tmp_path = tmp.name
    try:
        result = subprocess.run(
            ["node", "--check", tmp_path],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise AssertionError(
                f"JavaScript syntax error detected:\n{result.stderr}"
            )
    finally:
        os.unlink(tmp_path)


def test_ui_home_js_no_syntax_errors():
    with open(APP_PY, "r") as f:
        source = f.read()
    js_raw = extract_script_block(source)
    js = minimal_fstring_to_js(js_raw)
    validate_js_syntax(js)
    print("OK: ui_home() JavaScript has no syntax errors.")


if __name__ == "__main__":
    try:
        test_ui_home_js_no_syntax_errors()
        sys.exit(0)
    except AssertionError as e:
        print(f"FAIL: {e}", file=sys.stderr)
        sys.exit(1)
