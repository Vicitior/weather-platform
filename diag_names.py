# -*- coding: utf-8 -*-
# Diagnostic: where does the candidate list die?  Print counts at each stage
# of CharacterNameExtractor so we can see whether the entropy gate is too strict.
import io, math
from collections import defaultdict
from test_name_extract import SURNAMES, BAD_CHARS, MIN_LEN, MAX_LEN, is_plausible, entropy, is_cjk

MIN_DOC_COUNT = 3
MIN_NEIGHBOR_ENTROPY = 0.55

pages = []
for line in io.open('ocr_pages.txt', encoding='utf-8'):
    p = line.rstrip('\n').split('\t')
    if len(p) < 4:
        continue
    pages.append({'cid': p[0], 'title': p[1], 'page': int(p[2]), 'text': p[3]})

cand_freq = defaultdict(int)
cand_total = defaultdict(int)
left_nbr = defaultdict(lambda: defaultdict(int))
right_nbr = defaultdict(lambda: defaultdict(int))

for page in pages:
    text = page['text']
    if not text.strip():
        continue
    chars = [c for c in text if is_cjk(c)]
    if not chars:
        continue
    onp = set()
    for i in range(len(chars)):
        if chars[i] not in SURNAMES:
            continue
        for ln in range(MIN_LEN, MAX_LEN + 1):
            if i + ln > len(chars):
                break
            cand = ''.join(chars[i:i + ln])
            if not is_plausible(cand):
                break
            if cand not in onp:
                onp.add(cand)
                cand_freq[cand] += 1
            cand_total[cand] += 1
            l = chars[i - 1] if i > 0 else '^'
            r = chars[i + ln] if i + ln < len(chars) else '$'
            left_nbr[cand][l] += 1
            right_nbr[cand][r] += 1

print('stage1 candidates (doc>=1):', len(cand_freq))
ge3 = [(c, d) for c, d in cand_freq.items() if d >= MIN_DOC_COUNT]
print('stage1 candidates (doc>=%d): %d' % (MIN_DOC_COUNT, len(ge3)))
ge3.sort(key=lambda x: -x[1])
print('\ntop 30 by page-count BEFORE entropy gate:')
for c, d in ge3[:30]:
    le, re_ = entropy(left_nbr[c]), entropy(right_nbr[c])
    print('  %-6s doc=%-4d L=%.3f R=%.3f best=%.3f' % (c, d, le, re_, max(le, re_)))

passes = [(c, d) for c, d in ge3 if max(entropy(left_nbr[c]), entropy(right_nbr[c])) >= MIN_NEIGHBOR_ENTROPY]
print('\nafter entropy gate (>=%.2f): %d' % (MIN_NEIGHBOR_ENTROPY, len(passes)))
for c, d in passes[:30]:
    le, re_ = entropy(left_nbr[c]), entropy(right_nbr[c])
    print('  %-6s doc=%-4d L=%.3f R=%.3f' % (c, d, le, re_))
