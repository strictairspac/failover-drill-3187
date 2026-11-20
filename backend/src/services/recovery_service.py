from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified
from fastapi import HTTPException
from src.models.entities import RecoverySession
from src.services import recovery_engine as engine
from src.utils.serialize import record

# 分步动作 -> 引擎函数；全部同步计算，结果随会话状态落 MySQL
ACTIONS = {
    'transfer': lambda s, p: engine.transfer(s, p.get('src'), p.get('dst'), p.get('amount')),
    'commit': lambda s, p: engine.commit(s, p.get('txn')),
    'flush_page': lambda s, p: engine.flush_page(s, p.get('page')),
    'checkpoint_begin': lambda s, p: engine.checkpoint_begin(s),
    'checkpoint_end': lambda s, p: engine.checkpoint_end(s),
    'truncate': lambda s, p: engine.truncate(s, p.get('to_lsn')),
    'crash': lambda s, p: engine.crash(s),
    'restart': lambda s, p: engine.restart(s),
    'recover_step': lambda s, p: engine.recover_step(s),
    'recover_run': lambda s, p: engine.recover_run(s),
    'reset': lambda s, p: engine.reset(s),
}


def create_session(db, payload):
    name = payload.name.strip()
    if db.scalar(select(RecoverySession.id).where(RecoverySession.name == name)):
        raise HTTPException(409, '同名演练会话已存在')
    obj = RecoverySession(name=name, status='running', state=engine.new_state(name))
    db.add(obj)
    db.flush()
    return obj


def list_sessions(db):
    rows = db.scalars(select(RecoverySession).order_by(RecoverySession.id.desc()).limit(100))
    return [{'id': r.id, 'name': r.name, 'status': r.status, 'created_at': record(r)['created_at']}
            for r in rows]


def get_session(db, session_id, lock=False):
    q = select(RecoverySession).where(RecoverySession.id == session_id)
    if lock:
        q = q.with_for_update()
    obj = db.scalar(q)
    if obj is None:
        raise HTTPException(404, '恢复演练会话不存在')
    return obj


def session_view(obj):
    return {'id': obj.id, 'name': obj.name, 'created_at': record(obj)['created_at'],
            **engine.view(obj.state)}


def apply_step(obj, payload):
    fn = ACTIONS.get(payload.action)
    if fn is None:
        raise HTTPException(400, f'未知动作 {payload.action}')
    try:
        fn(obj.state, payload.params or {})
    except engine.RecoveryError as exc:
        raise HTTPException(400, str(exc))
    obj.status = obj.state['status']
    flag_modified(obj, 'state')
    return obj
