from pathlib import Path
p = Path('outputs/COT Reports.py')
s = p.read_text(encoding='utf-8')
audit = Path('work/audit_fragment.txt').read_text(encoding='utf-8-sig')
s = s.replace('def to_sheet_values', audit + 'def to_sheet_values')
p.write_text(s, encoding='utf-8')
compile(s, str(p), 'exec')
print('Syntax validation passed')
