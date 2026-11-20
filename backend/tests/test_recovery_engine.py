"""崩溃恢复演练引擎的纯 Python 自测：python3 backend/tests/test_recovery_engine.py
覆盖：一笔转账改两页、steal、no-force、模糊检查点并发、日志截断与误删丢失、
重启补齐已提交/撤掉未提交、恢复中再断电的幂等（不重复扣款）。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.services import recovery_engine as E  # noqa: E402

PASS = []


def check(name, cond, detail=''):
    assert cond, f'{name} 失败 {detail}'
    PASS.append(name)
    print(f'  ok {name}')


def balances(state):
    return {a['acct']: a['current'] for a in E.view(state)['accounts']}


def disk_balances(state):
    out = {}
    for p in state['disk']['pages'].values():
        out.update(p['balances'])
    return out


def test_transfer_touches_two_pages():
    s = E.new_state('t')
    txn = E.transfer(s, 'A', 'C', 100)
    ops = s['memory']['txns'][txn]['ops']
    check('一笔转账改两页', {o['page'] for o in ops} == {'P1', 'P2'}, str(ops))
    check('缓冲余额已改', balances(s) == {'A': 900, 'B': 800, 'C': 700, 'D': 400})
    check('磁盘未动', disk_balances(s) == E.INITIAL_BALANCES)


def test_no_force_commit():
    s = E.new_state('t')
    txn = E.transfer(s, 'A', 'C', 100)
    E.commit(s, txn)
    check('提交后磁盘页未刷（no-force）', disk_balances(s) == E.INITIAL_BALANCES)
    types = [r['type'] for r in s['disk']['wal']]
    check('提交强制刷日志到 commit', 'commit' in types and 'update' in types)
    E.crash(s)
    E.restart(s)
    E.recover_run(s)
    check('重启补齐已提交转账', balances(s) == {'A': 900, 'B': 800, 'C': 700, 'D': 400}, str(balances(s)))
    check('恢复无丢失', s['last_recovery']['lost'] == [])


def test_steal_uncommitted_page_flushed_then_undone():
    s = E.new_state('t')
    t1 = E.transfer(s, 'A', 'C', 100)
    E.commit(s, t1)
    E.transfer(s, 'B', 'D', 50)  # T2 不提交
    E.flush_page(s, 'P2')  # steal：未提交的 D=350 落盘
    check('未提交的页也能落盘（steal）', disk_balances(s)['D'] == 450)
    E.crash(s)
    E.restart(s)
    E.recover_run(s)
    check('撤掉未提交的改动', balances(s) == {'A': 900, 'B': 800, 'C': 700, 'D': 400}, str(balances(s)))
    check('败者被回滚', s['last_recovery']['undone_txns'] == ['T2'])


def test_crash_during_recovery_is_idempotent():
    s = E.new_state('t')
    t1 = E.transfer(s, 'A', 'C', 100)
    E.commit(s, t1)
    E.transfer(s, 'B', 'D', 50)
    E.flush_page(s, 'P2')
    E.crash(s)
    E.restart(s)
    # 恢复走几步后再次断电
    for _ in range(4):
        E.recover_step(s)
    check('恢复中可再断电', s['status'] == 'recovering')
    E.crash(s)
    E.restart(s)
    E.recover_run(s)
    check('重试恢复不重复扣款', balances(s) == {'A': 900, 'B': 800, 'C': 700, 'D': 400}, str(balances(s)))
    check('恢复中再断电后无丢失', s['last_recovery']['lost'] == [])


def test_crash_mid_undo_with_flushed_clr():
    s = E.new_state('t')
    E.transfer(s, 'B', 'D', 50)  # 未提交
    E.flush_page(s, 'P2')
    E.crash(s)
    E.restart(s)
    E.recover_step(s)  # analysis
    while s['recovery'] and s['recovery']['phase'] != 'undo':
        E.recover_step(s)
    E.recover_step(s)  # undo：写 CLR 并应用
    E.flush_page(s, 'P2')  # CLR 与页一起落盘
    E.crash(s)
    E.restart(s)
    E.recover_run(s)
    check('CLR 落盘后重试不重复回滚', balances(s) == E.INITIAL_BALANCES, str(balances(s)))


def test_fuzzy_checkpoint_concurrent_with_transfer():
    s = E.new_state('t')
    E.checkpoint_begin(s)
    t1 = E.transfer(s, 'A', 'C', 100)  # 检查点进行中继续转账
    E.commit(s, t1)
    E.checkpoint_end(s)
    check('检查点不刷数据页', disk_balances(s) == E.INITIAL_BALANCES)
    check('主记录指向检查点', s['disk']['master_checkpoint'] is not None)
    E.crash(s)
    E.restart(s)
    E.recover_run(s)
    check('检查点后崩溃恢复正确', balances(s) == {'A': 900, 'B': 800, 'C': 700, 'D': 400})


def test_safe_truncate_keeps_recovery_working():
    s = E.new_state('t')
    t1 = E.transfer(s, 'A', 'C', 100)
    E.commit(s, t1)
    E.flush_page(s, 'P1')
    E.flush_page(s, 'P2')
    E.checkpoint_begin(s)
    E.checkpoint_end(s)
    E.truncate(s)  # 安全截断
    check('安全截断后日志被清理', s['truncated_count'] > 0)
    E.crash(s)
    E.restart(s)
    E.recover_run(s)
    check('安全截断后恢复不丢账', balances(s) == {'A': 900, 'B': 800, 'C': 700, 'D': 400})
    check('安全截断后无丢失记录', s['last_recovery']['lost'] == [])


def test_unsafe_truncate_loses_committed_transfer():
    s = E.new_state('t')
    t1 = E.transfer(s, 'A', 'C', 100)
    E.commit(s, t1)  # 页未刷盘
    E.truncate(s, 9999)  # 误删：越过安全线
    E.crash(s)
    E.restart(s)
    E.recover_run(s)
    check('误删日志导致已提交转账丢失', balances(s) == E.INITIAL_BALANCES, str(balances(s)))
    lost = s['last_recovery']['lost']
    check('丢失被检测并报告', len(lost) == 1 and lost[0]['txn'] == t1, str(lost))
    check('台账应有额仍记录转账', s['last_recovery']['expected']['A'] == 900)


def test_active_txn_pins_log_retention():
    s = E.new_state('t')
    t1 = E.transfer(s, 'A', 'C', 100)
    E.commit(s, t1)
    E.flush_page(s, 'P1')
    E.flush_page(s, 'P2')  # 清空脏页，让未提交事务成为唯一压线因素
    t2 = E.transfer(s, 'B', 'D', 50)  # 未提交
    safe, pins = E.safe_point(s)
    first = s['memory']['txns'][t2]['first_lsn']
    check('未提交事务压住截断线', safe == first, f'safe={safe} first={first}')
    check('压线原因标注未提交事务', any(t2 in p['reason'] for p in pins), str(pins))
    v = E.view(s)
    t2_recs = [r for r in v['wal_disk'] if r.get('txn') == t2]
    check('留存原因可见', all(r['retain'] for r in t2_recs), str(t2_recs))
    E.truncate(s)  # 安全截断不能动 T2 的日志
    check('安全截断保留未提交事务日志',
          any(r.get('txn') == t2 for r in s['disk']['wal']) or
          any(r.get('txn') == t2 for r in s['memory']['wal_buffer']))


def test_commit_flushes_log_but_not_pages():
    s = E.new_state('t')
    txn = E.transfer(s, 'A', 'C', 100)
    E.commit(s, txn)
    check('commit 记录已落盘', any(r['type'] == 'commit' for r in s['disk']['wal']))
    check('数据页未落盘', all(p['page_lsn'] == 0 for p in s['disk']['pages'].values()))


def test_recover_with_nothing_to_do():
    s = E.new_state('t')
    E.crash(s)
    E.restart(s)
    E.recover_run(s)
    check('空日志恢复', balances(s) == E.INITIAL_BALANCES and s['status'] == 'running')


def test_guards():
    s = E.new_state('t')
    E.crash(s)
    try:
        E.transfer(s, 'A', 'C', 100)
        raise AssertionError('断电后转账应被拒绝')
    except E.RecoveryError:
        check('断电后拒绝转账', True)
    E.restart(s)
    E.recover_run(s)
    try:
        E.commit(s, 'T9')
        raise AssertionError('不存在的事务应被拒绝')
    except E.RecoveryError:
        check('提交不存在事务被拒绝', True)
    try:
        E.transfer(s, 'A', 'C', 999999)
        raise AssertionError('余额不足应被拒绝')
    except E.RecoveryError:
        check('余额不足被拒绝', True)


def test_account_locks_prevent_dirty_reads():
    s = E.new_state('t')
    E.transfer(s, 'A', 'C', 100)  # T1 未提交，锁住 A、C
    try:
        E.transfer(s, 'B', 'A', 50)
        raise AssertionError('脏读应被锁拒绝')
    except E.RecoveryError:
        check('未提交事务锁住账户', True)
    E.commit(s, 'T1')
    E.transfer(s, 'B', 'A', 50)  # 提交后锁释放
    check('提交后锁释放', balances(s) == {'A': 950, 'B': 750, 'C': 700, 'D': 400})


def test_continue_after_recovery_and_second_crash():
    s = E.new_state('t')
    t1 = E.transfer(s, 'A', 'C', 100)
    E.commit(s, t1)
    E.crash(s)
    E.restart(s)
    E.recover_run(s)
    t2 = E.transfer(s, 'B', 'D', 200)  # 恢复后继续营业
    E.commit(s, t2)
    E.flush_page(s, 'P1')
    E.flush_page(s, 'P2')
    E.crash(s)
    E.restart(s)
    E.recover_run(s)
    check('多轮崩溃后余额正确', balances(s) == {'A': 900, 'B': 600, 'C': 700, 'D': 600}, str(balances(s)))


def test_reset():
    s = E.new_state('t')
    t1 = E.transfer(s, 'A', 'C', 100)
    E.commit(s, t1)
    E.reset(s)
    check('重置回初始', balances(s) == E.INITIAL_BALANCES and s['disk']['wal'] == [])


if __name__ == '__main__':
    for name, fn in sorted({k: v for k, v in globals().items() if k.startswith('test_')}.items()):
        print(f'[{name}]')
        fn()
    print(f'\n全部通过：{len(PASS)} 项断言')
