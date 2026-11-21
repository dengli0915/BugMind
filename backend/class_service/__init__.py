# -*- coding: utf-8 -*-
"""班级体系服务：班级码邀请制 + 一人多班。"""

from .router import router
from .classes import CLASS_TABLES, generate_class_code, list_class_summary

__all__ = ["router", "CLASS_TABLES", "generate_class_code", "list_class_summary"]
