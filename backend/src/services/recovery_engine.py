"""ARIES 风格的崩溃恢复演练引擎：纯字典状态 + 纯函数，不依赖框架与数据库。

教学模型（非生产存储引擎），演示：
- WAL：先写日志再改数据页；提交只强制刷日志，不刷数据页（no-force）
- steal：未提交事务的脏页允许落盘，崩溃后由恢复回滚
- 模糊检查点：与转账并发，只快照事务表/脏页表，不刷数据页
- 日志截断：安全线 = min(未提交事务首LSN, 脏页recLSN, 检查点锚点)，误删会让已提交转账丢失
- 重启恢复：分析 → 重做（pageLSN 判断，幂等）→ 回滚（CLR + undoNext，幂等）
"""
from copy import deepcopy

PAGE_LAYOUT = {'P1': ['A', 'B'], 'P2': ['C', 'D']}  # 一笔跨页转账必然改两页
INITIAL_BALANCES = {'A': 1000, 'B': 800, 'C': 600, 'D': 400}
EVENT_CAP = 200


class RecoveryError(Exception):
    """演练操作不满足前提（如断电后转账）。路由层转为 400。"""


def new_state(name):
    return {
        'name': name,
        'status': 'running',  # running / crashed / recovering
        'initial': dict(INITIAL_BALANCES),
        'layout': {p: list(a) for p, a in PAGE_LAYOUT.items()},
        'disk': {
            'pages': {p: {'balances': {a: INITIAL_BALANCES[a] for a in accts}, 'page_lsn': 0}
                      for p, accts in PAGE_LAYOUT.items()},
            'wal': [],  # 已落盘日志，LSN 递增
            'master_checkpoint': None,  # 主记录：最近完成检查点的 begin LSN
        },
        'memory': _fresh_memory(),
        'lsn': 1,
        'txn_seq': 1,
        'ledger': [],  # 业务台账：已提交转账（在存储引擎之外，用于对照恢复结果）
        'recovery': None,
        'last_recovery': None,
        'truncated_count': 0,
        'events': [],
    }


def _fresh_memory():
    return {'buffer': {}, 'wal_buffer': [], 'txns': {}, 'checkpoint': None, 'locks': {}}


def _event(state, msg):
    state['events'].append(msg)
    del state['events'][:-EVENT_CAP]


def _require(state, statuses, msg):
    if state['status'] not in statuses:
        raise RecoveryError(msg)


def _append_log(state, rec):
    rec['lsn'] = state['lsn']
    state['lsn'] += 1
    txn = rec.get('txn')
    t = state['memory']['txns'].get(txn) if txn else None
    rec['prev_lsn'] = t['last_lsn'] if t else None
    state['memory']['wal_buffer'].append(rec)
    if t:
        t['last_lsn'] = rec['lsn']
    return rec


def _flush_log_upto(state, lsn):
    """把 LSN<=给定值的日志记录刷到磁盘（WAL 的唯一强制点）。"""
    buf = state['memory']['wal_buffer']
    move = [r for r in buf if r['lsn'] <= lsn]
    state['disk']['wal'].extend(move)
    state['memory']['wal_buffer'] = [r for r in buf if r['lsn'] > lsn]
    return len(move)


def _page_of(state, acct):
    for p, accts in state['layout'].items():
        if acct in accts:
            return p
    raise RecoveryError(f'账户 {acct} 不存在')


def _load_page(state, page):
    buf = state['memory']['buffer']
    if page not in buf:
        disk_page = state['disk']['pages'][page]
        buf[page] = {'balances': dict(disk_page['balances']), 'page_lsn': disk_page['page_lsn'],
                     'dirty': False, 'rec_lsn': None}
    return buf[page]


def _dirty(state, page, lsn):
    p = state['memory']['buffer'][page]
    if not p['dirty']:
        p['dirty'] = True
        p['rec_lsn'] = lsn


def _txn_no(rec):
    t = rec.get('txn')
    return int(t[1:]) if t and t.startswith('T') and t[1:].isdigit() else 0


# ---------- 正常运行的分步动作 ----------

def transfer(state, src, dst, amount):
    """一笔转账改两页：先写 WAL，再改缓冲页。"""
    _require(state, ('running',), '系统已断电或正在恢复，无法发起转账')
    src, dst = str(src or '').upper(), str(dst or '').upper()
    if src == dst:
        raise RecoveryError('转出与转入账户不能相同')
    for a in (src, dst):
        if a not in state['initial']:
            raise RecoveryError(f'账户 {a} 不存在（可选 A/B/C/D）')
    try:
        amount = int(amount)
    except (TypeError, ValueError):
        raise RecoveryError('金额必须是正整数')
    if amount <= 0:
        raise RecoveryError('金额必须为正整数')
    if _load_page(state, _page_of(state, src))['balances'][src] < amount:
        raise RecoveryError(f'{src} 余额不足')
    locks = state['memory']['locks']
    for a in (src, dst):
        if a in locks:
            raise RecoveryError(f'账户 {a} 被未提交事务 {locks[a]} 锁定，等它提交或断电后再试')
    txn = f"T{state['txn_seq']}"
    state['txn_seq'] += 1
    begin = _append_log(state, {'type': 'begin', 'txn': txn})
    state['memory']['txns'][txn] = {'status': 'active', 'first_lsn': begin['lsn'], 'last_lsn': begin['lsn'],
                                    'src': src, 'dst': dst, 'amount': amount, 'ops': []}
    locks[src] = locks[dst] = txn
    pages = []
    for acct, delta in ((src, -amount), (dst, amount)):
        pg = _page_of(state, acct)
        bp = _load_page(state, pg)
        before, after = bp['balances'][acct], bp['balances'][acct] + delta
        rec = _append_log(state, {'type': 'update', 'txn': txn, 'page': pg, 'acct': acct,
                                  'before': before, 'after': after})
        bp['balances'][acct] = after
        bp['page_lsn'] = rec['lsn']
        _dirty(state, pg, rec['lsn'])
        state['memory']['txns'][txn]['ops'].append(
            {'page': pg, 'acct': acct, 'before': before, 'after': after, 'lsn': rec['lsn']})
        if pg not in pages:
            pages.append(pg)
    _event(state, f"{txn} 转账 {src}→{dst} {amount}：改页 {'、'.join(pages)}（先写 WAL 再改缓冲页，两页均在内存）")
    return txn


def commit(state, txn):
    """提交确认只强制刷日志，不等两页刷完（no-force）。"""
    _require(state, ('running',), '系统已断电或正在恢复，无法提交')
    t = state['memory']['txns'].get(txn)
    if not t or t['status'] != 'active':
        raise RecoveryError(f'事务 {txn} 不存在或已结束（断电会清空内存事务表）')
    crec = _append_log(state, {'type': 'commit', 'txn': txn})
    flushed = _flush_log_upto(state, crec['lsn'])
    t['status'] = 'committed'
    _append_log(state, {'type': 'end', 'txn': txn})
    state['memory']['locks'] = {a: h for a, h in state['memory']['locks'].items() if h != txn}
    state['ledger'].append({'txn': txn, 'src': t['src'], 'dst': t['dst'], 'amount': t['amount'],
                            'lsns': [op['lsn'] for op in t['ops']],
                            'pages': [op['page'] for op in t['ops']]})
    _event(state, f"{txn} 提交：强制刷 {flushed} 条日志到 LSN {crec['lsn']} 即返回确认，"
                  '两页数据仍留在缓冲池（no-force）；END 记录暂存日志缓冲区')


def flush_page(state, page):
    """刷页落盘：先补刷日志（WAL 规则），未提交的页也允许落盘（steal）。"""
    _require(state, ('running', 'recovering'), '断电中无法刷页，请先重启恢复')
    buf = state['memory']['buffer']
    if page not in state['layout']:
        raise RecoveryError(f'页 {page} 不存在')
    if page not in buf or not buf[page]['dirty']:
        raise RecoveryError(f'页 {page} 不在缓冲池或是干净的，无需刷盘')
    bp = buf[page]
    flushed = _flush_log_upto(state, bp['page_lsn'])
    state['disk']['pages'][page] = {'balances': dict(bp['balances']), 'page_lsn': bp['page_lsn']}
    bp['dirty'] = False
    bp['rec_lsn'] = None
    steal = [t for t, i in state['memory']['txns'].items()
             if i['status'] == 'active' and any(op['page'] == page for op in i['ops'])]
    msg = f"刷页 {page}：先补刷 {flushed} 条日志（WAL：日志先于数据页），页落盘 pageLSN={bp['page_lsn']}"
    if steal:
        msg += f"；页内含未提交事务 {'、'.join(steal)} 的改动也一并落盘（steal 策略）"
    _event(state, msg)


def checkpoint_begin(state):
    """模糊检查点开始：只快照事务表与脏页表，转账可继续并发。"""
    _require(state, ('running',), '当前状态不能做检查点')
    if state['memory']['checkpoint']:
        raise RecoveryError('已有检查点在进行中')
    rec = _append_log(state, {'type': 'begin_checkpoint'})
    txns = {t: {'status': i['status'], 'last_lsn': i['last_lsn']}
            for t, i in state['memory']['txns'].items()}
    dpt = {p: b['rec_lsn'] for p, b in state['memory']['buffer'].items() if b['dirty']}
    state['memory']['checkpoint'] = {'begin_lsn': rec['lsn'], 'txns': txns, 'dpt': dpt}
    _event(state, f"模糊检查点开始 LSN {rec['lsn']}：快照事务表 {list(txns) or '空'}、脏页表 {dpt or '空'}；"
                  '不阻塞转账、不刷数据页')


def checkpoint_end(state):
    _require(state, ('running',), '当前状态不能做检查点')
    ck = state['memory']['checkpoint']
    if not ck:
        raise RecoveryError('请先开始检查点')
    _append_log(state, {'type': 'end_checkpoint', 'txns': deepcopy(ck['txns']), 'dpt': dict(ck['dpt'])})
    flushed = _flush_log_upto(state, state['lsn'] - 1)  # 检查点记录本身必须落盘
    state['disk']['master_checkpoint'] = ck['begin_lsn']
    state['memory']['checkpoint'] = None
    _event(state, f"检查点完成：主记录指向 LSN {ck['begin_lsn']}（补刷 {flushed} 条日志；数据页不刷，模糊检查点）")


def safe_point(state):
    """安全截断线：min(未提交事务首LSN, 脏页recLSN, 检查点锚点)。None 表示全部可清。"""
    pins = []
    for t, i in state['memory']['txns'].items():
        if i['status'] == 'active':
            pins.append({'lsn': i['first_lsn'], 'reason': f'{t} 未提交，其日志用于崩溃后回滚'})
    for p, b in state['memory']['buffer'].items():
        if b['dirty']:
            pins.append({'lsn': b['rec_lsn'], 'reason': f'页 {p} 是脏页，重做起点 recLSN={b["rec_lsn"]}'})
    if state['disk']['master_checkpoint']:
        pins.append({'lsn': state['disk']['master_checkpoint'], 'reason': '最近检查点锚点（恢复入口）'})
    safe = min((p['lsn'] for p in pins), default=None)
    return safe, pins


def truncate(state, to_lsn=None):
    """截断磁盘日志。默认截到安全线；越过安全线属于误删，崩溃后已提交转账会丢失。"""
    _require(state, ('running',), '断电或恢复中不能截断日志')
    safe, _ = safe_point(state)
    wal = state['disk']['wal']
    if to_lsn is None:
        to_lsn = safe if safe is not None else max((r['lsn'] for r in wal), default=0) + 1
    try:
        to_lsn = int(to_lsn)
    except (TypeError, ValueError):
        raise RecoveryError('截断点必须是整数 LSN')
    keep = [r for r in wal if r['lsn'] >= to_lsn]
    n = len(wal) - len(keep)
    if n == 0:
        raise RecoveryError(f'LSN<{to_lsn} 没有可清理的磁盘日志')
    state['disk']['wal'] = keep
    state['truncated_count'] += n
    if safe is not None and to_lsn > safe:
        _event(state, f'⚠ 危险截断到 LSN {to_lsn}：越过安全线 LSN {safe}，删掉 {n} 条恢复仍需要的日志，'
                      '崩溃后对应已提交转账将丢失')
    else:
        _event(state, f'安全截断：清理 LSN<{to_lsn} 的 {n} 条旧日志（安全线 '
                      f'{"LSN " + str(safe) if safe is not None else "无，全部可清"}）')


# ---------- 断电与恢复 ----------

def crash(state):
    _require(state, ('running', 'recovering'), '已经处于断电状态')
    during_recovery = state['status'] == 'recovering'
    state['memory'] = _fresh_memory()
    wal = state['disk']['wal']
    state['lsn'] = max((r['lsn'] for r in wal), default=0) + 1
    state['txn_seq'] = max((_txn_no(r) for r in wal), default=0) + 1
    state['recovery'] = None
    state['status'] = 'crashed'
    _event(state, '⚡ 恢复过程中再次断电！内存中的恢复进度丢失，磁盘保持原样'
           if during_recovery else
           '⚡ 断电！缓冲页、日志缓冲区、内存事务表全部丢失；磁盘保留最后落盘状态')


def restart(state):
    _require(state, ('crashed',), '只有断电后才能重启')
    state['status'] = 'recovering'
    state['last_recovery'] = None
    state['recovery'] = {'phase': 'analysis', 'txn_table': {}, 'dpt': {}, 'redo_index': None,
                         'undo_queue': {}, 'stats': {'redone': 0, 'skipped': 0, 'clrs': 0}}
    _event(state, '重启：进入 ARIES 三段恢复（分析→重做→回滚），可逐步执行；恢复中可再断电验证幂等')


def recover_step(state):
    _require(state, ('recovering',), '不在恢复状态')
    rec = state['recovery']
    if rec['phase'] == 'analysis':
        _analysis(state)
    elif rec['phase'] == 'redo':
        _redo_step(state)
    else:
        _undo_step(state)


def recover_run(state):
    _require(state, ('recovering',), '不在恢复状态')
    for _ in range(10000):
        if state['status'] != 'recovering':
            return
        recover_step(state)
    raise RecoveryError('恢复步数异常，已中止')


def _analysis(state):
    rec = state['recovery']
    wal = state['disk']['wal']
    master = state['disk']['master_checkpoint']
    txns, dpt, start = {}, {}, 0
    anchored = False
    if master is not None:
        idx = next((i for i, r in enumerate(wal) if r['lsn'] == master and r['type'] == 'begin_checkpoint'), None)
        if idx is None:
            _event(state, f'⚠ 检查点锚点 LSN {master} 已被截断，退化为全量扫描')
        else:
            end = next((r for r in wal[idx + 1:] if r['type'] == 'end_checkpoint'), None)
            if end:
                txns = {t: dict(i) for t, i in end['txns'].items()}
                dpt = dict(end['dpt'])
            start = idx
            anchored = True
            _event(state, f'分析：从检查点锚点 LSN {master} 开始（快照事务 {list(txns) or "空"}，脏页表 {dpt or "空"}）')
    if not anchored:
        _event(state, '分析：无可用检查点，从头扫描磁盘日志')
    for r in wal[start:]:
        t = r.get('txn')
        if r['type'] == 'begin':
            txns[t] = {'status': 'active', 'last_lsn': r['lsn']}
        elif r['type'] in ('update', 'clr'):
            txns.setdefault(t, {'status': 'active', 'last_lsn': r['lsn']})
            txns[t]['last_lsn'] = r['lsn']
            dpt.setdefault(r['page'], r['lsn'])
        elif r['type'] == 'commit':
            txns.setdefault(t, {'status': 'committed', 'last_lsn': r['lsn']})
            txns[t].update(status='committed', last_lsn=r['lsn'])
        elif r['type'] == 'end':
            txns.pop(t, None)
    for t, i in txns.items():
        if i['status'] == 'committed' and not any(
                x.get('txn') == t and x['type'] in ('update', 'clr') for x in wal):
            _event(state, f'⚠ {t} 有提交记录但更新日志缺失（被截断），已提交修改无法重做')
    winners = [t for t, i in txns.items() if i['status'] == 'committed']
    losers = [t for t, i in txns.items() if i['status'] != 'committed']
    for t in winners:
        _append_log(state, {'type': 'end', 'txn': t})
    rec['txn_table'] = txns
    rec['dpt'] = dpt
    if dpt:
        redo_from = min(dpt.values())
        rec['redo_index'] = next((i for i, r in enumerate(wal) if r['lsn'] >= redo_from), len(wal))
    else:
        rec['redo_index'] = len(wal)
    rec['phase'] = 'redo'
    _event(state, f"分析完成：胜者 {winners or '无'}（补写 END），败者 {losers or '无'}；"
                  f"重做起点 {'LSN ' + str(min(dpt.values())) if dpt else '日志末尾'}")


def _redo_step(state):
    rec = state['recovery']
    wal = state['disk']['wal']
    i = rec['redo_index'] if rec['redo_index'] is not None else 0
    if i >= len(wal):
        rec['undo_queue'] = {t: i2['last_lsn'] for t, i2 in rec['txn_table'].items()
                             if i2['status'] != 'committed'}
        rec['phase'] = 'undo'
        _event(state, f"重做扫描结束（重做 {rec['stats']['redone']} 条，幂等跳过 {rec['stats']['skipped']} 条），"
                      f"进入回滚：败者 {list(rec['undo_queue']) or '无'}")
        if not rec['undo_queue']:
            _finish_recovery(state)
        return
    r = wal[i]
    rec['redo_index'] = i + 1
    if r['type'] in ('update', 'clr'):
        page = _load_page(state, r['page'])
        if page['page_lsn'] >= r['lsn']:
            rec['stats']['skipped'] += 1
            _event(state, f"重做 LSN {r['lsn']}：页 {r['page']} pageLSN={page['page_lsn']} ≥ {r['lsn']}，"
                          '跳过（幂等，不重复扣款）')
        else:
            value = r['after'] if r['type'] == 'update' else r['value']
            page['balances'][r['acct']] = value
            page['page_lsn'] = r['lsn']
            _dirty(state, r['page'], r['lsn'])
            rec['stats']['redone'] += 1
            kind = 'CLR 补偿' if r['type'] == 'clr' else '更新'
            _event(state, f"重做 LSN {r['lsn']}：{r['acct']} → {value}（页 {r['page']}，{kind}）")
    else:
        _event(state, f"重做扫描 LSN {r['lsn']}：{r['type']} 非数据记录，跳过")


def _find_record(state, lsn):
    for r in state['disk']['wal']:
        if r['lsn'] == lsn:
            return r
    for r in state['memory']['wal_buffer']:
        if r['lsn'] == lsn:
            return r
    return None


def _undo_step(state):
    rec = state['recovery']
    q = rec['undo_queue']
    if not q:
        _finish_recovery(state)
        return
    txn = max(q, key=lambda t: q[t] or 0)
    lsn = q[txn]
    r = _find_record(state, lsn) if lsn else None
    if r is None:
        if lsn:
            _event(state, f'⚠ 回滚 {txn} 需要的 LSN {lsn} 已被截断，该事务无法继续回滚（数据可能不一致）')
        else:
            _append_log(state, {'type': 'end', 'txn': txn})
            _event(state, f'{txn} 回滚完成，写 END')
        del q[txn]
    elif r['type'] == 'update':
        clr = _append_log(state, {'type': 'clr', 'txn': txn, 'page': r['page'], 'acct': r['acct'],
                                  'value': r['before'], 'undo_next': r.get('prev_lsn'), 'ref': r['lsn']})
        page = _load_page(state, r['page'])
        page['balances'][r['acct']] = r['before']
        page['page_lsn'] = clr['lsn']
        _dirty(state, r['page'], clr['lsn'])
        q[txn] = clr['lsn']
        rec['stats']['clrs'] += 1
        _event(state, f"回滚 {txn} 的 LSN {r['lsn']}：{r['acct']} 恢复为 {r['before']}"
                      f"（写 CLR LSN {clr['lsn']}，undo_next={r.get('prev_lsn')}）")
    elif r['type'] == 'clr':
        q[txn] = r.get('undo_next')
        _event(state, f"LSN {lsn} 是 CLR（已补偿过），沿 undo_next={r.get('undo_next')} 前进，不重复回滚")
    elif r['type'] == 'begin':
        _append_log(state, {'type': 'end', 'txn': txn})
        del q[txn]
        _event(state, f'{txn} 回滚到起点，写 END，事务了结')
    else:
        q[txn] = r.get('prev_lsn')
    if not q:
        _finish_recovery(state)


def _finish_recovery(state):
    rec = state['recovery']
    losers = [t for t, i in rec['txn_table'].items() if i['status'] != 'committed']
    balances = {}
    for p, accts in state['layout'].items():
        src = state['memory']['buffer'].get(p) or state['disk']['pages'][p]
        for a in accts:
            balances[a] = src['balances'][a]
    expected = _expected(state)
    wal_lsns = {r['lsn'] for r in state['disk']['wal']}
    lost = []
    for l in state['ledger']:
        for u, p in zip(l['lsns'], l['pages']):
            buf = state['memory']['buffer'].get(p)
            page_lsn = buf['page_lsn'] if buf else state['disk']['pages'][p]['page_lsn']
            if u not in wal_lsns and page_lsn < u:
                lost.append(l)
                break
    state['last_recovery'] = {
        'redone': rec['stats']['redone'], 'skipped': rec['stats']['skipped'],
        'clrs': rec['stats']['clrs'], 'undone_txns': losers,
        'balances': balances, 'expected': expected,
        'lost': [{'txn': l['txn'], 'src': l['src'], 'dst': l['dst'], 'amount': l['amount']} for l in lost],
    }
    state['recovery'] = None
    state['status'] = 'running'
    msg = (f"✔ 恢复完成：重做 {rec['stats']['redone']} 条、幂等跳过 {rec['stats']['skipped']} 条、"
           f"回滚 {len(losers)} 个事务（{'、'.join(losers) or '无'}）")
    if lost:
        msg += f"；⚠ 已提交转账丢失 {len(lost)} 笔：{'、'.join(l['txn'] for l in lost)}（日志被误删，无法重做）"
    _event(state, msg)


def _expected(state):
    expected = dict(state['initial'])
    for l in state['ledger']:
        expected[l['src']] -= l['amount']
        expected[l['dst']] += l['amount']
    return expected


def reset(state):
    name = state['name']
    state.clear()
    state.update(new_state(name))
    _event(state, '演练重置：账户、日志、检查点回到初始状态')


# ---------- 视图（含日志留存原因） ----------

def _retain_reasons(state, rec, safe):
    reasons = []
    master = state['disk']['master_checkpoint']
    if master is not None and rec['lsn'] == master:
        reasons.append('最近检查点锚点（恢复入口）')
    txn = rec.get('txn')
    if txn:
        t = state['memory']['txns'].get(txn)
        if t and t['status'] == 'active':
            reasons.append(f'{txn} 未提交，崩溃后回滚需要')
    if rec['type'] in ('update', 'clr'):
        b = state['memory']['buffer'].get(rec['page'])
        if b and b['dirty'] and rec['lsn'] >= (b['rec_lsn'] or 0):
            reasons.append(f"页 {rec['page']} 脏页未刷盘，重做需要")
    if not reasons and safe is not None and rec['lsn'] >= safe:
        reasons.append('在安全截断线之后')
    return reasons  # 空列表 = 可清理


def view(state):
    safe, pins = safe_point(state)
    accounts = []
    for p, accts in state['layout'].items():
        buf = state['memory']['buffer'].get(p)
        disk = state['disk']['pages'][p]
        for a in accts:
            accounts.append({'acct': a, 'page': p, 'initial': state['initial'][a],
                             'current': (buf or disk)['balances'][a],
                             'disk': disk['balances'][a], 'in_buffer': bool(buf)})
    return {
        'status': state['status'],
        'accounts': accounts,
        'expected': _expected(state),
        'txns': [dict({'txn': t}, **i) for t, i in state['memory']['txns'].items()],
        'locks': dict(state['memory']['locks']),
        'buffer': [dict({'page': p}, **b) for p, b in state['memory']['buffer'].items()],
        'disk_pages': [dict({'page': p}, **pg) for p, pg in state['disk']['pages'].items()],
        'wal_disk': [dict(r, retain=_retain_reasons(state, r, safe)) for r in state['disk']['wal']],
        'wal_buffer': deepcopy(state['memory']['wal_buffer']),
        'master_checkpoint': state['disk']['master_checkpoint'],
        'checkpoint_in_progress': bool(state['memory']['checkpoint']),
        'safe_truncate_lsn': safe,
        'retention_pins': pins,
        'recovery': state['recovery'],
        'last_recovery': state['last_recovery'],
        'ledger': deepcopy(state['ledger']),
        'truncated_count': state['truncated_count'],
        'events': list(reversed(state['events'][-60:])),
    }
