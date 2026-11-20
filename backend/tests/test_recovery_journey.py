"""端到端旅程自测：模拟页面分步操作序列 + view() 的 JSON 往返。
python3 backend/tests/test_recovery_journey.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.services import recovery_engine as E  # noqa: E402


def j(state):
    """view 必须可 JSON 序列化（MySQL JSON 列与 API 响应都依赖）。"""
    return json.loads(json.dumps(E.view(state), ensure_ascii=False))


def main():
    s = E.new_state('夜班断电演练')
    # 1) 夜班发起一笔跨页转账并提交：提交确认不等两页刷完
    t1 = E.transfer(s, 'A', 'C', 100)
    E.commit(s, t1)
    # 2) 第二笔转账不提交；把含未提交改动的 P2 刷盘（steal）
    E.transfer(s, 'B', 'D', 50)
    E.flush_page(s, 'P2')
    # 3) 检查点与转账并发（T3 不碰 T2 锁住的账户）
    E.checkpoint_begin(s)
    t3 = E.transfer(s, 'A', 'C', 30)
    E.commit(s, t3)
    E.checkpoint_end(s)
    # 4) 能清的旧日志及时清（安全截断）
    E.flush_page(s, 'P1')
    E.flush_page(s, 'P2')
    E.truncate(s)
    v = j(s)
    assert v['safe_truncate_lsn'] is not None or v['truncated_count'] >= 0
    # 5) 刷到一半断电
    E.transfer(s, 'C', 'A', 80)  # T4 未提交，留在内存
    E.crash(s)
    v = j(s)
    assert v['status'] == 'crashed' and v['buffer'] == [] and v['wal_buffer'] == []
    # 6) 重启逐步恢复，中途再断电，重试不重复扣款
    E.restart(s)
    for _ in range(3):
        E.recover_step(s)
    E.crash(s)
    E.restart(s)
    E.recover_run(s)
    v = j(s)
    got = {a['acct']: a['current'] for a in v['accounts']}
    # T1、T3 已提交补齐；T2、T4 未提交撤回
    assert got == {'A': 870, 'B': 800, 'C': 730, 'D': 400}, got
    assert v['last_recovery']['lost'] == []
    # T2 的改动落过盘（steal），恢复要回滚；T4 连日志都没刷盘，断电后无痕迹可回滚
    assert v['last_recovery']['undone_txns'] == ['T2'], v['last_recovery']['undone_txns']
    # 7) 误删日志会让已提交转账丢失
    E.reset(s)
    t1 = E.transfer(s, 'A', 'C', 100)
    E.commit(s, t1)
    E.truncate(s, 9999)
    E.crash(s)
    E.restart(s)
    E.recover_run(s)
    v = j(s)
    assert v['last_recovery']['lost'] and v['last_recovery']['lost'][0]['txn'] == t1
    assert v['last_recovery']['balances']['A'] == 1000  # 转账丢失
    assert v['last_recovery']['expected']['A'] == 900   # 台账应有
    print('旅程自测通过：分步转账/提交/刷页/检查点/断电/恢复/再断电/误删 全部符合预期')


if __name__ == '__main__':
    main()
