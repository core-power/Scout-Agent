#!/usr/bin/env python3
"""复验 11 个管理页 __tpl 动态模板的中文片段是否均可翻译。
语义与 i18n.js 的 __tpl/__t 完全一致:
- JS 转义解码(\\n 等)
- 精确整段匹配(不 trim)
- 前缀冒号回退: ^([\\u4e00-\\u9fff（）()，。、\\s]+)[::：]
用法: python3 -u scripts/verify_i18n_dynamic.py
"""
import re, sys, glob, os

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(BASE, "scout/web/static")
I18N = os.path.join(STATIC, "i18n.js")

def js_unescape(s):
    out = []
    i, n = 0, len(s)
    mapping = {'n': '\n', 't': '\t', 'r': '\r', 'b': '\b',
               'f': '\f', 'v': '\v', '\\': '\\', '"': '"', "'": "'", '0': '\0'}
    while i < n:
        c = s[i]
        if c == '\\' and i + 1 < n:
            nx = s[i + 1]
            if nx in mapping:
                out.append(mapping[nx]); i += 2; continue
            if nx == 'u' and i + 6 <= n:
                try:
                    out.append(chr(int(s[i + 2:i + 6], 16))); i += 6; continue
                except ValueError:
                    pass
            if nx == 'x' and i + 4 <= n:
                try:
                    out.append(chr(int(s[i + 2:i + 4], 16))); i += 4; continue
                except ValueError:
                    pass
        out.append(c); i += 1
    return ''.join(out)

def load_dict():
    js = open(I18N, encoding='utf-8').read()
    # 逐个提取 "key": { zh: (key 可含转义与真实换行), 线性扫描
    keys = set()
    i = 0
    n = len(js)
    while True:
        i = js.find('": { zh:', i)
        if i == -1:
            break
        # 向前找 key 起始引号(跳过转义)
        j = i
        while j > 0:
            k = js.rfind('"', 0, j)
            if k == -1:
                break
            # 检查该引号是否被反斜杠转义
            bs = 0
            t = k - 1
            while t >= 0 and js[t] == '\\':
                bs += 1; t -= 1
            if bs % 2 == 0:
                keys.add(js_unescape(js[k + 1:i]))
                j = k
                break
            j = k
        i += len('": { zh:')
    return keys

def can_translate(part, d):
    if part in d:
        return True
    m = re.match(r'^([\u4e00-\u9fff（）()，。、\s]+)[::：]', part)
    if m and m.group(1).strip() in d:
        return True
    return False

def find_tpl_args(html):
    """线性扫描所有 __tpl( 参数(支持反引号/单双引号与转义)"""
    args = []
    i = 0
    n = len(html)
    while True:
        i = html.find('__tpl(', i)
        if i == -1:
            break
        j = i + 6
        while j < n and html[j] in ' \t\r\n':
            j += 1
        if j < n and html[j] in '`"\'':
            q = html[j]
            k = j + 1
            while k < n:
                if html[k] == '\\':
                    k += 2; continue
                if html[k] == q:
                    break
                k += 1
            if k < n:
                args.append(html[j:k + 1])
        i = j + 1
    return args

def tpl_sim(arg, d):
    """模拟 __tpl(), 返回无法翻译的中文片段"""
    s = arg
    if s and s[0] in '`"\'' and len(s) >= 2:
        s = s[1:-1]
    s = js_unescape(s)
    if '${' in s:
        bad = []
        for part in re.split(r'(\$\{[^}]+\})', s):
            if part.startswith('${'):
                continue
            if re.search(r'[\u4e00-\u9fff]', part) and not can_translate(part, d):
                bad.append(part)
        return bad
    kv = re.match(r'^([\u4e00-\u9fff✅⚠️——，。、：\s]+?)\(([^()]*)\)([\u4e00-\u9fff✅⚠️——，。、：\s]+)$', s)
    if kv:
        if can_translate(kv.group(1), d) or can_translate(kv.group(3), d):
            return []
    if re.search(r'[\u4e00-\u9fff]', s) and not can_translate(s, d):
        return [s]
    return []

def main():
    d = load_dict()
    print(f"字典键数: {len(d)}", flush=True)
    total = 0
    for path in sorted(glob.glob(os.path.join(STATIC, "*.html"))):
        name = os.path.basename(path)
        html = open(path, encoding='utf-8').read()
        bad_list = []
        for arg in find_tpl_args(html):
            for b in tpl_sim(arg, d):
                bad_list.append(repr(b))
        if bad_list:
            print(f"[{name}] 无法翻译片段 {len(bad_list)} 个:", flush=True)
            for b in bad_list:
                print(f"    {b}", flush=True)
            total += len(bad_list)
    if total == 0:
        print("OK: 所有 __tpl 动态模板的中文片段均可翻译", flush=True)
        return 0
    print(f"\n共 {total} 处需处理", flush=True)
    return 1

if __name__ == '__main__':
    sys.exit(main())
