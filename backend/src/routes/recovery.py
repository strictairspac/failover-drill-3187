from fastapi import APIRouter, Depends
from src.config.database import get_db
from src.middlewares.auth import current_user, writable
from src.schemas.recovery import SessionInput, StepInput
from src.services.recovery_service import (create_session, list_sessions, get_session,
                                           session_view, apply_step)
from src.controllers.actions import audit

router = APIRouter()


@router.get('/recovery/sessions')
def listing(db=Depends(get_db), user=Depends(current_user)):
    return list_sessions(db)


@router.post('/recovery/sessions', status_code=201)
def create(data: SessionInput, db=Depends(get_db), user=Depends(writable)):
    obj = create_session(db, data)
    audit(db, user, f'recovery:create:{obj.id}')
    db.commit()
    return session_view(obj)


@router.get('/recovery/sessions/{session_id}')
def detail(session_id: int, db=Depends(get_db), user=Depends(current_user)):
    return session_view(get_session(db, session_id))


@router.post('/recovery/sessions/{session_id}/steps')
def step(session_id: int, data: StepInput, db=Depends(get_db), user=Depends(writable)):
    obj = get_session(db, session_id, lock=True)  # 行锁保证分步动作串行
    apply_step(obj, data)
    audit(db, user, f'recovery:{data.action}:{session_id}')
    db.commit()
    return session_view(obj)
