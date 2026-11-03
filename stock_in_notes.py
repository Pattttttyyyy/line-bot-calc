"""Extract an explicit note from the command/header, preserving serial content."""
import re


def extract_stock_in_note(raw_lines):
    lines = list(raw_lines)
    if not lines:
        return lines, ''
    # Only parse a space-delimited # on the command line. Never scan card lines.
    marker = re.search(r'\s+#', lines[0])
    if not marker:
        return lines, ''
    note = lines[0][marker.end():].strip()
    lines[0] = lines[0][:marker.start()].rstrip()
    if not note:
        raise ValueError('備註不可空白；請在 # 後填供貨來源，例如 #EP。')
    if len(note) > 200:
        raise ValueError('備註最多 200 字，請縮短後再入庫。')
    return lines, note
