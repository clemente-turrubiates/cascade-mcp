import csv
from collections import Counter, defaultdict

with open('agent_logs.csv', newline='', encoding='utf-8-sig') as f:
    rows = list(csv.DictReader(f))

print(f'total rows: {len(rows)}')
print(f'columns ({len(rows[0])}): {list(rows[0].keys())}')

regimes = sorted(set(r['regime'] for r in rows))
seeds = sorted(set(r['seed'] for r in rows))
tags = sorted(set(r['policy_tag'] for r in rows))
print(f'regimes ({len(regimes)}): {regimes}')
print(f'seeds:   {seeds}')
print(f'policy_tags ({len(tags)}): {tags}')

bad = [r for r in rows if len(r) != 26]
print(f'malformed rows: {len(bad)}')
print(f'null arms: {sum(1 for r in rows if not r["arm"])}')

# per-regime x policy_tag arm distribution
print()
hdr = (f'{"regime":<18} {"policy_tag":<18} {"n":>7} '
       f'{"WINNER":>7} {"FORK":>6} {"OCC_CMT":>7} {"RECOMP":>7} {"OCC_ABT":>7} '
       f'{"rec/c":>6} {"sil":>5} {"win%":>6} {"rec%":>6}')
print(hdr); print('-' * len(hdr))

by = defaultdict(list)
for r in rows:
    by[(r['regime'], r['policy_tag'])].append(r)

for regime in regimes:
    for tag in tags:
        grp = by.get((regime, tag))
        if not grp: continue
        arms = Counter(r['arm'] for r in grp)
        n = len(grp)
        rec = sum(int(r['recomputes']) for r in grp)
        sil = sum(int(r['silent_error']) for r in grp)
        w = arms.get('WINNER', 0) + arms.get('OCC_COMMIT', 0)
        rc = arms.get('RECOMPUTE', 0) + arms.get('OCC_ALLABORT', 0)
        print(f'{regime:<18} {tag:<18} {n:>7} '
              f'{arms.get("WINNER",0):>7} {arms.get("FORK",0):>6} '
              f'{arms.get("OCC_COMMIT",0):>7} {arms.get("RECOMPUTE",0):>7} '
              f'{arms.get("OCC_ALLABORT",0):>7} {rec/n:>6.2f} {sil:>5} '
              f'{100*w/n:>5.1f}% {100*rc/n:>5.1f}%')
    print()