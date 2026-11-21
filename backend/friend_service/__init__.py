# -*- coding: utf-8 -*-
"""好友体系（申请 / 收件箱 / 同意拒绝 / 好友列表）。

数据落在 后端/data/friends.json 与 后端/data/friend_requests.json，
与作业、班级共用 homework_service.store 的原子读写层。
"""

from .router import router

__all__ = ["router"]
