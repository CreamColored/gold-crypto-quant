"""SQLAlchemy 声明式模型的公共基类。"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """所有数据库模型共享的基类，用于集中收集表元数据。"""
