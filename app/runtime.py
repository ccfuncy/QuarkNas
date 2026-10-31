from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request

from .config_store import ConfigStore
from .db import DB
from .download.queue import TaskRunner
from .events import EventBus
from .quark.auth import AccountStore
from .quark.qrlogin import QrLoginManager
from .settings import Settings


@dataclass
class Ctx:
    settings: Settings
    config: ConfigStore
    db: DB
    bus: EventBus
    account: AccountStore
    runner: TaskRunner
    qr: QrLoginManager


def get_ctx(request: Request) -> Ctx:
    return request.app.state.ctx
