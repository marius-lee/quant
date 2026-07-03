"""一次性修复所有 Python 文件的格式问题:
  1. Tab → 4 spaces
  2. 移除行尾空白
  3. 文件末尾确保一个换行符
"""
import os

ROOT = os.path.dirname(os.path.dirname(__file__))
EXCLUDE = {'.venv', '.claude', '.git', '__pycache__'}

fixed_files = 0

for dirpath, dirnames, filenames in os.walk(ROOT):
    # Skip excluded dirs
    dirnames[:] = [d for d in dirnames if d not in EXCLUDE]
    for fname in filenames:
        if not fname.endswith('.py'):
            continue
        fpath = os.path.join(dirpath, fname)
        with open(fpath, 'r') as f:
            lines = f.readlines()

        changed = False
        new_lines = []
        for line in lines:
            # 1. Tab → 4 spaces (preserving relative indentation)
            new_line = line.expandtabs(4)
            # 2. Remove trailing whitespace
            new_line = new_line.rstrip() + '\n' if new_line.rstrip() or line.endswith('\n') else new_line.rstrip()
            if new_line != line:
                changed = True
            new_lines.append(new_line)

        # 3. Ensure exactly one trailing newline
        if new_lines and not new_lines[-1].endswith('\n'):
            new_lines[-1] += '\n'
            changed = True
        # Remove extra trailing newlines
        while len(new_lines) > 1 and new_lines[-1] == '\n' and new_lines[-2].endswith('\n'):
            new_lines.pop()
            changed = True

        if changed:
            with open(fpath, 'w') as f:
                f.writelines(new_lines)
            fixed_files += 1
            print(f"  Fixed: {os.path.relpath(fpath, ROOT)}")

print(f"\n{'-' * 50}")
print(f"Total: {fixed_files} files fixed")
